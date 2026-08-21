# Pokémon Draft League Bot

A Discord bot for running Pokémon draft leagues. Every channel can host its own
completely independent draft, everything is stored in SQLite so a reboot never
loses progress, and Google Sheets is an optional mirror rather than a
requirement.

---

## Contents

1. [How it works](#how-it-works)
2. [Project layout](#project-layout)
3. [Running it on your own machine](#running-it-on-your-own-machine)
4. [Getting a bot token](#getting-a-bot-token)
5. [Hosting on an Oracle Cloud VM](#hosting-on-an-oracle-cloud-vm)
6. [Backups](#backups)
7. [Command reference](#command-reference)
8. [Google Sheets](#google-sheets)
9. [Design decisions](#design-decisions)
10. [Tests](#tests)
11. [Troubleshooting](#troubleshooting)

---

## How it works

**One draft per channel.** Every command looks up its draft using the channel it
was typed in. `#vgc` and `#national-dex-singles` can run different drafts at the
same time, with different players, budgets and rosters, and they can never see
each other's data. The same person can play in both with separate teams.

**SQLite is the source of truth.** Picks, budgets, timers and trades are written
to `draft.db` before anything is announced. Google Sheets is updated afterwards
and in the background, so if Google is slow or down the draft simply carries on.

**Snake order is generated from the player list.** Round 1 runs 1→N, round 2 runs
N→1, and so on, for any number of players from 2 to 16.

---

## Project layout

| File | What it does |
|---|---|
| `bot.py` | Entry point. Wires up the database, timers, sheets and cogs. |
| `config.py` | Every setting you might want to change, in one place. |
| `database.py` | The SQLite schema, connection and backup helpers. |
| `draft.py` | **All the draft rules.** No Discord code, so it is easy to test. |
| `timers.py` | One countdown per active draft. |
| `sheets.py` | Optional Google Sheets reading and writing. |
| `ui.py` | Turns draft events into Discord messages. |
| `cogs/admin.py` | `/setup` `/edit` `/draft` `/canceldraft` `/sync` `/backup` `/inspectsheet` |
| `cogs/drafting.py` | `/pick` `/queuepick` `/makeuppick` `/editpick` `/status` `/progress` `/roster` |
| `cogs/extras.py` | `/trade` `/approvetrade` `/rejecttrade` `/trades` `/search` `/available` |
| `tests/` | Tests for the rules and for the Discord layer. |

`draft.py` returns lists of plain dictionaries called **events**
(`{"type": "skipped", ...}`). `ui.py` turns each one into a message. That split
is why the rules can be tested without a Discord connection.

---

## Running it on your own machine

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then paste your token into .env
python3 bot.py
```

Google Sheets packages are listed in `requirements.txt` but are optional. If they
are missing the bot logs `Google Sheets is disabled: ...` and everything else
works normally.

---

## Getting a bot token

1. Go to <https://discord.com/developers/applications> and click **New
   Application**. Give it a name.
2. Open the **Bot** tab, then **Reset Token** → **Copy**. This is the only time
   Discord shows it to you.
3. Paste it into `.env`:

   ```
   DISCORD_TOKEN=your.token.goes.here
   ```

   Never commit `.env`. It is already listed in `.gitignore`. If the token ever
   leaks, press **Reset Token** and paste the new one in.

4. Still on the Bot tab, no privileged intents are required. The bot uses slash
   commands only, so leave *Message Content Intent* and *Server Members Intent*
   switched off.

5. Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: **Send Messages**, **Embed Links**, **Read Message History**
   - Open the generated URL and invite the bot to your server.

6. While testing, set `DEV_GUILD_ID` in `.env` to your server's ID. Commands then
   appear immediately instead of taking up to an hour to register globally.
   (Right-click your server → Copy Server ID, with Developer Mode enabled.)

---

## Hosting on an Oracle Cloud VM

These steps assume an Oracle Cloud **Ubuntu 22.04** ARM or x86 instance, which is
what the Always Free tier gives you. Any Linux VM works the same way.

### 1. Create the instance

In the Oracle Cloud console: **Compute → Instances → Create Instance**, pick an
Always Free eligible shape and the Ubuntu image, and **download the SSH private
key** when prompted. The bot only makes outbound connections, so you do **not**
need to open any inbound ports beyond SSH.

### 2. Connect and install Python

```bash
ssh -i /path/to/your-key.key ubuntu@YOUR_INSTANCE_IP
```

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
```

### 3. Copy the bot across

Either clone it from your own git repository:

```bash
git clone https://github.com/YOUR_NAME/OCDL-Bot.git ~/OCDL-Bot
```

or copy the folder up from your own machine:

```bash
scp -i /path/to/your-key.key -r ./OCDL-Bot ubuntu@YOUR_INSTANCE_IP:~/
```

### 4. Install dependencies

```bash
cd ~/OCDL-Bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

### 5. Add your token on the VM

Create the `.env` file directly on the server so the token never sits in your
git history:

```bash
nano ~/OCDL-Bot/.env
```

Put this in it, save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`:

```
DISCORD_TOKEN=your.token.goes.here
DEV_GUILD_ID=
GOOGLE_CREDENTIALS_FILE=/home/ubuntu/OCDL-Bot/credentials.json
```

Lock the file down so only your user can read it:

```bash
chmod 600 ~/OCDL-Bot/.env
```

> Prefer not to keep a token in a file at all? Put it in the systemd unit
> instead, with `Environment="DISCORD_TOKEN=..."` under `[Service]`, and run
> `sudo chmod 600 /etc/systemd/system/draftbot.service`. The bot reads the
> environment variable first and the `.env` file second.

### 6. Check it starts

```bash
cd ~/OCDL-Bot && ./.venv/bin/python bot.py
```

You should see `logged in as YourBot#1234`. Stop it with `Ctrl+C`.

### 7. Run it as a service so it survives reboots

```bash
sudo cp ~/OCDL-Bot/draftbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now draftbot
```

Useful commands from then on:

```bash
sudo systemctl status draftbot     # is it running?
journalctl -u draftbot -f          # live logs
sudo systemctl restart draftbot    # after changing the code
```

`Restart=always` in the unit file means systemd restarts the bot if it crashes,
and `enable` means it starts again automatically when the VM reboots. On startup
the bot reloads every active draft from SQLite and puts the current player back
on the clock, so a reboot costs you nothing.

---

## Backups

* The bot writes a timestamped copy of the database to `backups/` every
  `BACKUP_INTERVAL_HOURS` (default 6) and keeps the newest `BACKUPS_TO_KEEP`
  (default 40).
* `/backup` makes one immediately.
* Backups use SQLite's own backup API, which is safe to run while a draft is
  live — unlike copying the file with `cp`.

Copy them off the VM now and then:

```bash
scp -i /path/to/your-key.key -r ubuntu@YOUR_INSTANCE_IP:~/OCDL-Bot/backups ./
```

To restore, stop the bot, replace `draft.db` with a backup, and start it again:

```bash
sudo systemctl stop draftbot
cp ~/OCDL-Bot/backups/draft-2026-08-21_09-00-00.db ~/OCDL-Bot/draft.db
sudo systemctl start draftbot
```

---

## Command reference

### Setting up

| Command | Who | What it does |
|---|---|---|
| `/setup` | Manage Server | Creates a draft in this channel. Collects budget, min/max picks, moderator role, base timer, admin channel, players, max trades per player, and an optional sheet link. Does **not** start the draft. |
| `/edit` | Moderator | Changes budget, min picks, max picks or sheet link. Locked once `/draft` runs. |
| `/draft` | Moderator | Starts the draft, pings the first player and starts their timer. |
| `/canceldraft` | Moderator | Cancels this channel's draft. Data is kept, not deleted. |

Players are given as @mentions in draft order:
`players: @Larry @Alex @Sarah` — between 2 and 16 of them.

### Drafting

| Command | Who | What it does |
|---|---|---|
| `/pick round pokemon cost` | Player on the clock (or a moderator) | Makes a pick, announces it, and moves to the next player. Pokémon names autocomplete when a sheet is linked. |
| `/queuepick round pokemon [costs]` | Any player | Queues up to 5 Pokémon in priority order for one of your upcoming turns. `pokemon: Garchomp, Heatran, Skarmory` |
| `/makeuppick round pokemon cost` | Any player | Resolves a round you were skipped in. Does not change whose turn it is. |
| `/editpick round pokemon cost [player]` | Own picks; moderators for anyone | Replaces a pick, correcting roster, budget, availability and history. A moderator can also use it to *add* a pick for a round the player has nothing recorded in. |

### Looking things up

`/status`, `/progress [round]` and `/roster [player]` all reply **only to you**,
so nobody spams the channel. `/progress` with no round shows every completed
round; with a round it shows that round's picks in true chronological order.

### Trades

| Command | Who | What it does |
|---|---|---|
| `/trade drops gets [partner]` | Any player | Proposes a trade (up to 5 each way) and sends it to the admin channel with a trade ID. With `partner` it is a player-to-player swap; without, a free-agency move. Nothing changes yet. |
| `/approvetrade trade_id [channel]` | Moderator | Applies the trade to rosters and budgets. A trade can only be approved once. |
| `/rejecttrade trade_id [channel]` | Moderator | Rejects it. Nothing changes. |
| `/trades [channel]` | Anyone | Lists this draft's trades and their status. |

Trade IDs count from 1 **per draft**, so `#league-a` and `#league-b` each have
their own trade #1. Approve from the admin channel by naming the draft channel:
`/approvetrade trade_id:1 channel:#league-a`.

### Google Sheets

`/search`, `/available`, `/sync` and `/inspectsheet` need a linked sheet — see
below.

---

## Google Sheets

Sheets support is **optional**. A draft with no sheet works completely
normally, and a sheet that stops responding never blocks a pick.

### What the bot reads

**`Pokédex`** — column **E** is the Pokémon name used in every command, column
**K** is its cost. Costs are read live at draft time, so a league that copies
the template and re-prices it works with no code changes.

A Pokémon is **banned** — not draftable — when its cost cell is blank, holds
text (`B`, `Banned`, `-`, …), or holds a placeholder number listed in
`config.BANNED_COST_VALUES` (99 and 999 by default).

The **`Tier List`** tab is deliberately *not* read. It is generated from the
same Pokédex costs, so reading both could only ever produce disagreement.
(Checked against the template: 848 draftable Pokémon, zero disagreements
between the two tabs.)

### What the bot writes

Only two things. Everything else in the document is formulas that react.

| Where | What | Why |
|---|---|---|
| `Coaches!B2:B17` | Player usernames, in draft order | Every other tab refers back to these cells, so writing here updates coach names everywhere |
| `Rosters` — leftmost column of each coach block | The Pokémon name for each round | The columns either side hold image and cost formulas driven by that name |

The 16 roster blocks are listed in `config.SHEET_LAYOUT["roster_blocks"]` in
coach order: `C4, J4, Q4, X4, C19, …, X49`. Each is 12 rows tall, one row per
round, and each row is merged across four columns — so the bot writes the
leftmost cell only, and never touches the image column to its left or the
cost column to its right.

**How a block is filled.** A Pokémon the coach drafted sits in its own round's
row, so the block reads like the draft did. A Pokémon dropped in a trade simply
leaves its row empty, and anything gained drops into the first free row,
repeating until the block is full. If trades ever leave a roster with more
Pokémon than a block has rows, `/sync` says whose roster no longer fits and by
how much — the database still holds everything, and `/roster` still shows it
all.

Because each block is 12 rows tall, **a sheet-linked draft can have at most 12
rounds**. `/setup` refuses a larger `max_picks` rather than silently losing
picks off the bottom of a block.

### What it adds in Discord

* Pokémon names **autocomplete** in `/pick`, `/makeuppick` and `/editpick`,
  showing the cost and offering only undrafted, unbanned Pokémon.
* `/pick` checks the cost against the Pokédex, and rejects banned Pokémon.
* `/search` reports whether a Pokémon is drafted, banned, or available.
* `/available` lists undrafted Pokémon at a given point value.
* `/sync` rewrites the coach names and every roster block from the database.

### Connecting one

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project and enable the **Google Sheets API** and **Google Drive API**.
2. Create a **service account**, then **Keys → Add key → JSON**. Save the file
   as `credentials.json` next to `bot.py`.
3. Open the JSON file and copy the `client_email` value.
4. Share your copy of the spreadsheet with that email address as **Editor**.
5. Link the sheet with `/setup` or `/edit`, then restart the bot.

### Before the bot writes anything

`config.SHEETS_WRITE_ENABLED` is **`False`** by default. While it is off the
bot reads the Pokédex (for autocomplete, `/search`, `/available` and cost
checks) but never writes. Confirm the layout first:

1. Run **`/inspectsheet`**. It reports the tabs, how many Pokémon are
   draftable versus banned, the point values in use, and the roster block
   layout — without changing anything.
2. If anything disagrees, correct `SHEET_LAYOUT` in `config.py`.
3. Then set `SHEETS_WRITE_ENABLED = True` and restart.

Nothing about the layout is guessed. If a tab named in `SHEET_LAYOUT` is
missing, the bot says exactly which one and lists what the document actually
contains, rather than writing to the wrong place.

### Budgets

The template works each coach's remaining budget out from whatever is on
their roster (`=115-SUM(...)` in the stock template). The bot keeps its own
budget the same way, so the two agree — including after trades. If your copy
uses a different starting budget, set the same number in `/setup`.

## Design decisions

Where the requirements left room for interpretation, this is what was chosen.
All of it is easy to change in `config.py` or `draft.py`.

**When the draft ends.** After `max_picks` rounds, **or** as soon as no player
can afford another pick — whichever comes first. A player who has filled their
roster or cannot afford the cheapest possible Pokémon
(`config.MINIMUM_PICK_COST`) is passed over: no skip, no makeup pick, no timer
penalty, and the draft simply moves on.

**Skips, makeup picks and timers** follow this sequence:

```
missed pick → turn skipped → timer halved (unless a queue was set)
            → makeup pick due before the next turn
            → no makeup pick → that next turn is skipped too → timer halved again
```

Each forfeited turn creates a fresh makeup pick for the round just lost, so a
player never accumulates more than one outstanding debt — and a player who
keeps ignoring it keeps being skipped, which is what stops one absent drafter
stalling everybody else. Making any makeup pick puts them straight back to
normal. A turn is only ever halved **once**, even when the expiry and the
forfeit land together, so 120 → 60 → 30 → 15 stays one step per turn.

**At a snake round boundary, two lost rounds cost one halving.** The player at
each round's turn picks twice in a row — seat 16 picks last in round 1 and
first in round 2. If they are skipped on the first of those, their makeup
deadline (their next turn) arrives immediately, so both rounds are lost. The
pair only ever reduces their timer **once**, because they never had a moment
in which to use the makeup pick. And if the reason they were skipped was a
queue that got sniped, the pair costs them **nothing** — the timer is left
alone for both rounds.

**An exhausted queue never costs a player their timer.** If every Pokémon they
queued has already been drafted, the bot says so publicly and the normal timer
runs so they can still pick by hand. If that timer then expires they are
skipped and owe a makeup pick as usual, but their timer is **not** reduced and
their skip count does not increase.

**Trades are judged by admins, not by the bot.** `/trade` records what was
asked for and sends it to the admin channel; the only hard limits are the size
of each side and the per-player cap from `/setup` (`max_trades`, 0 for
unlimited). Anything that looks wrong — trading a Pokémon you do not own, or
claiming one that is already drafted — is attached to the admin's message as a
warning rather than blocking the proposal. With a `partner` it is a two-player
swap; without one it is a free-agency move against the undrafted pool, and the
cost of anything picked up is filled in from the Pokédex.

**History and rosters are separate.** The `picks` table is the permanent record
of who drafted what and when — trades never rewrite it, so `/progress` always
shows what really happened. The `rosters` table is current ownership, and it is
what decides availability, so a Pokémon dropped in a trade correctly becomes
draftable again. It is also what gets written to the spreadsheet, which is why
the sheet's budget column stays right after a trade.

**Names are stored exactly as the Pokédex spells them.** Whatever a player
types, the bot resolves it against the sheet and records the sheet's own
spelling. The roster block feeds VLOOKUPs for the Pokémon image and its cost,
and the coach's budget is worked out from those — so a name that did not match
the Pokédex would leave the sheet unable to price the pick. Without a linked
sheet the player's own spelling is kept.

**Admins can create picks, not just edit them.** `/editpick` on a round where
the player has nothing recorded will add the pick, and settle any makeup pick
they owed for that round. Players themselves get pointed at `/makeuppick`
instead.

**A restart never skips anybody.** Turn deadlines are stored in the database.
If the bot was offline when a deadline passed, the current player's timer is
restarted from scratch and the channel is told, because downtime is the host's
fault and not the drafter's.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

101 tests, no Discord connection and no network needed. They cover snake order
for every player count from 2 to 16, round transitions, channel isolation,
timer halving and the makeup cascade, the snake-boundary case (one halving for
two lost rounds, none at all if the queue was sniped), queued picks and the
exhausted-queue exception, duplicates, wrong rounds, budgets, pick limits,
ending early when everyone is broke, trade caps, admin-created picks, roster
blocks after trades, Pokédex name resolution and banned Pokémon, behaviour
during a Google outage, restart recovery, the spreadsheet cell mapping, and a
randomised full-draft simulation that checks the invariants that matter
(nobody owns the same Pokémon twice, budgets never go negative, spent +
remaining always equals the starting budget, pick order has no gaps).

## Troubleshooting

**Slash commands do not appear.** Global registration can take up to an hour. Set
`DEV_GUILD_ID` in `.env` to your server ID and restart for instant registration.
Also check the bot was invited with the `applications.commands` scope.

**"There is no draft in this channel."** Commands only ever see the draft in the
channel they were typed in. Run them in the draft's own channel, or use the
`channel:` option where one is offered.

**The bot cannot post.** Give it **Send Messages** in both the draft channel and
the admin channel.

**A pick worked but the sheet did not update.** That is by design — the pick is
already saved locally. Check `journalctl -u draftbot -f` for the Google error,
then run `/sync` once it is fixed.

**Nothing happens when a timer runs out.** Check the bot is actually running
(`sudo systemctl status draftbot`). Timers only run while the process is alive;
after a restart the current player's clock is restarted rather than skipped.
