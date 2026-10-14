# Player command list

Everything below is written for **players**, not moderators. Copy each block
and paste it into your league's channel — they are each kept under Discord's
2000-character message limit, so paste them as separate messages.

Nothing here lists the moderator commands. Those are in the README.

---

## Block 1 — drafting

```
**📋 How to draft**

**`/pick`** — make your pick when it is your turn.
`/pick round:1 pokemon:Great Tusk cost:18`
• `round` must match the round we are actually in.
• `cost` must match the tier list. If a sheet is linked, the Pokémon name will autocomplete — use it, it gets the spelling right.
• If anything is wrong, only *you* see the error. Nothing is recorded until it is valid.

**`/queuepick`** — set a backup list for a later turn, in case you are away.
`/queuepick round:3 pokemon:Garchomp, Heatran, Skarmory`
• Up to 5, best first. When your turn comes the bot takes the first one still available.
• Add `costs: 12, 10, 8` if no sheet is linked.
• **If every Pokémon in your queue gets taken, your timer is NOT reduced.** You keep your normal time to pick by hand.

**`/myqueue`** — check what you have queued. Only you can see the reply.
`/myqueue round:3`  •  `/myqueue` on its own shows every round you queued.

**`/cancelqueue`** — throw a queue away before it fires.
`/cancelqueue round:3`  •  `/cancelqueue` on its own clears every round you queued.
• Changed your mind, or queued the wrong thing? This deletes it. No pick is undone and your clock is untouched.

**`/makeuppick`** — use a turn you were skipped in.
`/makeuppick round:3 pokemon:Garchomp cost:12`
• You get one chance: it must be used **before your next turn**.

**`/finishdrafting`** — stop early and keep the team you have, once you are past the league's minimum. It shows what you would give up and asks you to confirm. Only a moderator can put you back on the clock.

**`/editpick`** — fix your own pick, **until the next pick is made**.
`/editpick round:2 pokemon:Heatran cost:10`
• After somebody else has picked it is too late — ask a moderator, who can fix it any time.
• This applies to a pick your queue made for you too, so check it when you get back.
```

---

## Block 2 — the timer and being skipped

```
**⏱️ The timer — read this one**

You each have your **own** timer. Everybody starts at the base time.

**If you miss your turn:**
1. You are skipped, and **your timer is halved** — e.g. 2m → 1m → 30s → 15s. It does not go back up when you pick again.
2. You owe a **makeup pick** for that round, and you must use `/makeuppick` **before your next turn**.
3. If you do not, that next turn is skipped too and your timer halves again.

So one missed turn is recoverable. Ignoring the makeup pick is what spirals.

**Two things that do NOT cost you time:**
• Your `/queuepick` list being completely drafted out from under you.
• Being at the turn of the snake — if you pick twice in a row and get skipped on the first, you lose both rounds but your timer is only reduced **once**.

A reduced timer is not permanent punishment — ask a moderator if you think it is wrong.
```

---

## Block 3 — checking things

```
**🔎 Looking things up** *(these reply only to you, so use them freely)*

**`/roster`** — your team: Pokémon, costs, points spent, points left, picks used.
`/roster player:@someone` to see somebody else's.

**`/status`** — whose turn it is, which round, how long they have left, and any outstanding makeup picks.

**`/progress`** — every pick made so far, in order.
`/progress round:3` for just one round.

**`/search pokemon:Garchomp`** — is it still available, and what does it cost?

**`/available cost:12`** — every undrafted Pokémon at that price.
```

---

## Block 4 — trades and substitutions *(skip for a draft-only league)*

```
**🤝 Trades and subs**

**`/trade`** — propose a trade. Up to 5 Pokémon each way.
`/trade drops:Garchomp gets:Heatran partner:@Alex`
• With `partner` it is a swap with that player.
• **Without** `partner` it is free agency: you drop Pokémon back into the pool and pick up undrafted ones.
• Dropping nothing? Put `nil` or `0` in `drops` to pay for a pickup with the points you have left: `/trade drops:nil gets:Heatran`
• Nothing changes until a moderator approves it, and then **rosters change when the next week is posted**, so nothing moves in the middle of a week. You will see the trade number.
• The league's limit counts the **Pokémon you pick up**, not the ones you drop.
• For **24 hours after the draft ends** (the bot pings everyone), trades are unlimited and don't count toward the limit.

• The bot checks your spelling against the tier list, and that you own what you are dropping and can afford what you are taking, **before** a moderator sees it. If it refuses, fix the names — `/search` finds the right spelling.

**`/trades`** — see every trade and whether it was approved.

**`/subrequest reason:...`** — ask for a substitution.
• Already drafting? This asks to **sub out**.
• Not in the draft? This offers to **sub in**.
• A moderator reads it and decides. `/subrequest reason:exams next week`
```

---

## Block 5 — Tera formats only

Only paste this if your league has a **Tera budget**. Otherwise skip it.

```
**⭐ Tera Captains**

You have two budgets: your normal points, and a separate **Tera budget**. You may have at most **3 Tera Captains**, and no minimum.

Add `tera_captain: True` to a pick to make it a captain:
`/pick round:1 pokemon:Great Tusk cost:17 tera_captain:True`

**The cost is different, and it comes out of both budgets.** A captain is charged its **Tera price**, which replaces the normal price and can be lower:

• Great Tusk normally — 18 points, Tera budget untouched.
• Great Tusk as a captain — **17 points from your normal budget AND 17 from your Tera budget.**

Use `/search` to see both prices. Some Pokémon have no Tera price at all — they are **Tera banned** and can never be captains.

`/roster` shows your Tera budget and which of your team are captains.
```

---

## Block 6 — new: checking and cancelling your queue

```
**📋 Two new commands for your queue**

`/queuepick` lines up picks for a turn you might miss. Now you can check and cancel them too.

**`/myqueue`** — see what you have queued.
`/myqueue round:3` — just that round.
`/myqueue` — every round you have queued.
• Lists them in priority order with their costs, and marks any ⭐ Tera Captains.
• **Only you can see the reply.** Your queue stays your business.

**`/cancelqueue`** — delete a queue before it fires.
`/cancelqueue round:3` — just that round.
`/cancelqueue` — every round you have queued.
• For when you have changed your mind, or queued the wrong Pokémon.
• **It costs you nothing.** No pick is undone, your clock is not touched, and it is not a skip. You can even run it while you are on the clock.
• That turn then needs a real `/pick` — or a fresh `/queuepick`.

**Worth knowing:** `/queuepick` now checks your costs against the tier list and refuses a wrong price instead of quietly drafting at it. Leave `costs` out entirely and the bot fills in the list's own prices.

**Also changed:** `/editpick` now only works until the next pick is made. After that, ask a moderator — they can fix anything at any time.
```
