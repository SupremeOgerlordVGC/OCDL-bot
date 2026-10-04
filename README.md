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
8. [Documents](#documents)
9. [Design decisions](#design-decisions)
10. [Tests](#tests)
11. [Troubleshooting](#troubleshooting)

---

## How it works

**A league, one or two pools.** You create a league by running `/setup` in the
channel where you want trades approved — that becomes the league's **admin
channel**. The draft itself happens in the **pool channel** (or two) you name
in the command. League names are unique per server, so one admin channel can
look after several leagues at once.

**Pools draft independently.** Two pools run at the same time, each with its
own snake order, turn and timers, and the same Pokémon may be taken once in
each. Only the draft is split: the season schedule can pair coaches across
pools.

**SQLite is the source of truth.** Picks, budgets, timers and trades are
written to `draft.db` before anything is announced. The spreadsheet is updated
afterwards and in the background, so if Google is slow or down the draft simply
carries on.

**Snake order is generated from the player list.** Round 1 runs 1→N, round 2
runs N→1, and so on, for any number of players from 2 up.

**Documents are optional.** A league can run with no document at all, with a
simple tiering sheet, or with the full league template — see
[Documents](#documents).

## Project layout

| File | What it does |
|---|---|
| `bot.py` | Entry point. Wires up the database, timers, sheets and cogs. |
| `config.py` | Every setting you might want to change, in one place. |
| `database.py` | The SQLite schema, connection and backup helpers. |
| `draft.py` | **All the draft rules.** No Discord code, so it is easy to test. |
| `timers.py` | One countdown per active pool. |
| `sheets.py` | Optional Google Sheets reading and writing. |
| `ui.py` | Turns draft events into Discord messages. |
| `cogs/admin.py` | `/setup` `/edit` `/draft` `/canceldraft` `/archive` `/draft-moderator` `/postschedule` `/sync` `/inspectsheet` `/backup` |
| `cogs/drafting.py` | `/pick` `/queuepick` `/makeuppick` `/editpick` `/status` `/progress` `/roster` |
| `cogs/extras.py` | `/trade` `/approvetrade` `/rejecttrade` `/trades` `/subrequest` `/substitute` `/search` `/available` |
| `tests/` | Tests for the rules, the sheet mapping and the Discord layer. |

The database keeps leagues and pools separate, and a SQL view called
`pool_view` flattens a pool together with its league's settings — which is why
the rules can read `ctx["budget"]` or `ctx["max_picks"]` without joining
anything by hand.

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
| `/setup` | Moderator | Creates a league **in the admin channel you run it from**. Takes a name, mode, pool channel(s) with their own player lists, budgets, pick limits, timer, trade cap and an optional document. Does **not** start the draft. |
| `/edit` | Moderator | Changes budgets, pick limits, trade cap or the document link. Locked once `/draft` runs. |
| `/draft` | Moderator | Run in a pool's channel: starts that pool, pings the first player and begins their timer. |
| `/draft-moderator` | Administrator | Grants or revokes a role's access to every moderator command on the server. Run with no options to list the current roles. |
| `/canceldraft` | Moderator | Cancels a league by name. Data is kept, not deleted. |
| `/archive` | Moderator | Closes a finished league and frees its channels. Draft-only leagues do this themselves. |

Players are given as @mentions in draft order:
`players: @Larry @Alex @Sarah`. The first mention is coach 1, which is also
their roster block on the spreadsheet. A second pool needs both
`draft_channel_b` and `players_b`, and a template.

Nobody can use the moderator commands until an Administrator has run
`/draft-moderator`. Administrators and the server owner always have access.

### Drafting

| Command | Who | What it does |
|---|---|---|
| `/pick round pokemon cost [tera_captain]` | Player on the clock (or a moderator) | Makes a pick, announces it, and moves to the next player. Names autocomplete when a document is linked. |
| `/queuepick round pokemon [costs] [tera]` | Any player | Queues up to 5 Pokémon in priority order for one of your upcoming turns. `tera` is a matching yes/no list. |
| `/makeuppick round pokemon cost [tera_captain]` | Any player | Resolves a round you were skipped in. Does not change whose turn it is. |
| `/editpick round pokemon cost [tera_captain] [player]` | Own picks; moderators for anyone | Replaces a pick, correcting roster, budgets, availability and history. A moderator can also use it to *add* a pick for a round with nothing recorded. |

### Looking things up

`/status`, `/progress [round]` and `/roster [player]` reply **only to you**, so
nobody spams the channel. `/progress` with no round shows every completed
round; with a round it shows that round's picks in true chronological order.

### Trades and substitutions

Both are proposed in a pool channel and sent to the league's admin channel.

| Command | Who | What it does |
|---|---|---|
| `/trade drops gets [partner]` | Any player | Proposes a trade (up to 5 each way). With `partner` it is a player-to-player swap; without, a free-agency move against the undrafted pool. Nothing changes yet. |
| `/approvetrade trade_id [name]` | Moderator | Applies the trade to rosters and budgets. A trade can only be approved once. |
| `/rejecttrade trade_id [name]` | Moderator | Rejects it. Nothing changes. |
| `/trades [name]` | Anyone | Lists a league's trades and their status. |
| `/subrequest reason` | Anyone | Asks to leave the draft (if you are in it) or to join it (if you are not). Posted for the moderators. |
| `/substitute out into` | Moderator | Hands a seat over: the roster, budgets and any makeup picks stay, and only the person changes. |

Trades and substitution requests are numbered **per league**, so an admin
channel shared by several leagues never has two #1s in it. Add `name:` when
that channel runs more than one league.

Trades are a full-season feature; a draft-only league refuses them.

### Season

| Command | Who | What it does |
|---|---|---|
| `/postschedule week name` | Moderator | Posts that week's matchups into the channel you run it in, pinging both players. Needs a template and full-season mode. Never automatic. |
| `/sync` | Moderator | Rewrites the coach list and every roster block from the database. |
| `/inspectsheet` | Moderator | Reports the document's structure without changing anything. |
| `/backup` | Moderator | Saves a timestamped copy of the database. |

## Documents

A league can run three ways, and you pick at most one document.

| | No document | Tiering sheet | Template |
|---|---|---|---|
| Draft, timers, makeup picks, trades | ✅ | ✅ | ✅ |
| Prices checked, names autocompleted | — | ✅ | ✅ |
| `/search`, `/available` | — | ✅ | ✅ |
| Tera Captains | ✅ (you type the price) | ✅ | ✅ |
| Rosters written to the sheet | — | — | ✅ |
| Settings read from the sheet | — | — | ✅ |
| Two pools, `/postschedule` | — | — | ✅ |

A sheet that stops responding never blocks a pick — the draft carries on using
the local database.

### A tiering sheet

The simplest useful document, and nothing fancy is required:

| | A | B | C |
|---|---|---|---|
| **1** | *(headers, ignored)* | | |
| **2** | `Great Tusk` | `18` | `17` |
| **3** | `Garchomp` | `17` | |

Column **A** is the Pokémon name, **B** its price, **C** its Tera price. Reading
starts at row 2. That is the one shape the bot understands, and it is also how
it reads prices out of the full template.

A Pokémon is **banned** — not draftable at all — when column B is blank, holds
text (`B`, `Banned`, `-`, …) or holds a placeholder number listed in
`config.BANNED_COST_VALUES` (99 and 999 by default). A Pokémon with no Tera
price is **Tera banned**: draftable, but never a Tera Captain.

### The league template

`/setup` reads the league's own configuration from the template, through its
**named ranges** rather than fixed cells, so the template can move them:

| Named range | Used for |
|---|---|
| `Budget` | Points each coach may spend |
| `TeraBudget` + `TeraFormat` | Tera points, and whether it is a Tera format at all |
| `NumPools` | 1 or 2 |
| `TieringSheet` | **Which tab holds the price list** |
| `SeasonWeeks` | Season length |

Because the template declares its own budget, `/setup` does not ask for one —
and refuses an option that contradicts the sheet, so the two can never drift.
`TieringSheet` means a league can re-price simply by pointing at a different
tiering tab.

The bot **writes only two things**; everything else in the document is formulas
that react to them:

| Where | What |
|---|---|
| `Coaches!B` and `Coaches!Y` | Usernames and pool letters. Every other tab refers back to these cells, so writing here updates coach names everywhere. |
| The leftmost column of each roster block | One Pokémon name per round — plus the Tera Captain tick box in a Tera format. |

The 20 roster blocks are listed in `config.SHEET_LAYOUT["roster_blocks"]` in
coach order. Each is 12 rows tall, one row per round, and the bot writes only
the name cell and (in a Tera format) the tick box three columns to its right —
never the image formula to the left or the cost formula to the right.

Because each block is 12 rows tall, **a template-linked draft can have at most
12 rounds**, and `/setup` refuses more rather than silently losing picks.

### Tera Captains

A Tera format is turned on by a non-zero Tera budget. Each team may have up to
`config.MAX_TERA_CAPTAINS` captains (3) and needs no minimum.

A captain is charged its **Tera price from both budgets**, and that price
*replaces* the normal one — it is not a surcharge, and it can be lower. With a
115 budget and a 28 Tera budget, and Great Tusk at 18 / Tera 17:

| Pick | Budget | Tera budget |
|---|---|---|
| Great Tusk, normal | 115 − 18 = **97** | **28** (untouched) |
| Great Tusk, captain | 115 − 17 = **98** | 28 − 17 = **11** |

That is exactly what the template's own formula does: the Rosters tab subtracts
every row, and the Tera budget subtracts only the ticked ones. In a Tera format
the bot writes to the **`Tera Rosters`** tab; otherwise to **`Rosters`**.

### Connecting a document

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project and enable the **Google Sheets API** and **Google Drive API**.
2. Create a **service account**, then **Keys → Add key → JSON**. Save the file
   as `credentials.json` next to `bot.py`.
3. Open that file and copy the `client_email` value.
4. **Make your own copy** of the template (File → Make a copy), then share the
   copy with that email address as **Editor**.
5. Link it in `/setup` with `template_link:` or `tiering_link:` — one or the
   other, never both.
6. Run **`/inspectsheet`**. It reads only, and reports the tabs, the settings it
   found, which tiering tab is in use, how many Pokémon are draftable versus
   banned, how many may be captains, and which roster tab picks would go to.

If a tab named in `SHEET_LAYOUT` is missing, the bot says exactly which one and
lists what the document actually contains, rather than writing to the wrong
place. Set `config.SHEETS_WRITE_ENABLED = False` for a dry run: everything else
keeps working and the bot simply stops writing.

## Design decisions

Where the requirements left room for interpretation, this is what was chosen.
All of it is easy to change in `config.py` or `draft.py`.

**One league, one or two pools.** The league owns the name, budgets, document,
mode and admin channel; a pool owns the turn pointer, timer and players. Two
pools require a template, because the pool assignment lives on its Coaches tab.
Pool A takes roster blocks 1–10 and pool B 11–20, which matches the "Pool A" and
"Pool B" headings on the Rosters tab and caps a two-pool league at ten coaches
each. A single pool can use all 20.

**When the draft ends.** After `max_picks` rounds, **or** as soon as no player
can afford another pick — whichever comes first. A player who has filled their
roster or cannot afford the cheapest possible Pokémon
(`config.MINIMUM_PICK_COST`) is passed over: no skip, no makeup pick, no timer
penalty. A `draft_only` league archives itself at that point and frees its
channels; a `full_season` league stays open for trades and the schedule.

**Skips, makeup picks and timers** follow this sequence:

```
missed pick → turn skipped → timer halved (unless a queue was set)
            → makeup pick due before the next turn
            → no makeup pick → that next turn is skipped too → timer halved again
```

Each forfeited turn creates a fresh makeup pick for the round just lost, so a
player never carries more than one outstanding debt — and a player who keeps
ignoring it keeps being skipped, which stops one absent drafter stalling
everybody. Making any makeup pick puts them straight back to normal. A turn
costs at most **one** halving, so 120 → 60 → 30 → 15 stays one step per turn.

**At a snake round boundary, two lost rounds cost one halving.** The player at
each round's turn picks twice in a row — seat 16 picks last in round 1 and first
in round 2. Skipped on the first of those, their makeup deadline arrives
immediately, so both rounds are lost; the pair reduces their timer only once,
because they never had a moment in which to use the makeup. If the reason they
were skipped was a queue that got sniped, the pair costs them **nothing**.

**An exhausted queue never costs a player their timer.** If every Pokémon they
queued has already been drafted, the bot says so publicly and the normal timer
runs so they can still pick by hand. If that timer expires they are skipped and
owe a makeup pick as usual, but their timer is not reduced and their skip count
does not increase.

**Tera Captains are priced from the Tera column, against both budgets.** The
Tera price replaces the normal one rather than adding to it, and may be lower.
A Pokémon with no Tera price cannot be a captain. Maximum three per team, no
minimum. This works with no document at all, since `/pick` carries the price —
a mistake is fixed with `/editpick`.

**Trades are judged by moderators, not by the bot.** `/trade` records what was
asked for and sends it to the admin channel; the only hard limits are the size
of each side and the per-player cap from `/setup` (`max_trades`, 0 for
unlimited). Anything that looks wrong — trading a Pokémon you do not own, or
claiming one already drafted — is attached to the moderator's message as a
warning rather than blocking the proposal.

**History and rosters are separate.** The `picks` table is the permanent record
of who drafted what and when — trades never rewrite it, so `/progress` always
shows what really happened. The `rosters` table is current ownership, and it is
what decides availability, so a Pokémon dropped in a trade becomes draftable
again. It is also what gets written to the spreadsheet, which is why the sheet's
budget column stays right after a trade. A Pokémon gained in a trade has no
round of its own, so it fills the first free row in the block.

**Names are stored exactly as the tiering list spells them.** Whatever a player
types, the bot resolves it against the list and records the list's own spelling.
The roster block feeds VLOOKUPs for the image and the price, and the coach's
budget is worked out from those — so a name that did not match would leave the
sheet unable to price the pick. Without a document the player's spelling is kept.

**A substitution hands over the seat, not the team.** The roster, budgets, draft
position and any makeup picks all stay exactly as they were; only the person
changes. Their timer and skip count reset to the base, because a replacement
should not inherit somebody else's penalties — and a moderator has to approve
the swap, so that cannot be used to dodge a shrinking clock.

**Moderator roles are per server, not per league.** One set of people can look
after every league running on it. Administrators and the server owner always
have access.

**Moderators can create picks, not just edit them.** `/editpick` on a round
where the player has nothing recorded will add the pick and settle any makeup
pick they owed for it. Players themselves get pointed at `/makeuppick`.

**A restart never skips anybody.** Turn deadlines are stored in the database. If
the bot was offline when a deadline passed, the current player's timer is
restarted from scratch and the channel is told, because downtime is the host's
fault and not the drafter's.

**The bot will not run a clock nobody can see.** If it cannot post in a pool's
channel — usually a missing **View Channel** or **Send Messages** permission —
the turn is paused rather than timed, because skipping players for a permission
problem and halving their timers would be unfair. Picks, rosters and budgets are
still recorded normally. Grant the permission and restart the bot: the paused
turn opens again and the player is pinged.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

146 tests, no Discord connection and no network needed. They cover snake order
for every player count, round transitions, channel and pool isolation, timer
halving and the makeup cascade, the snake-boundary case (one halving for two
lost rounds, none if the queue was sniped), queued picks and the
exhausted-queue exception, duplicates, wrong rounds, budgets, pick limits,
ending early when everyone is broke, Tera Captain budgets and the three-captain
cap, Tera-banned Pokémon, two-pool independence, league name uniqueness, trade
caps, substitutions, draft-only archiving, moderator-created picks, roster
blocks after trades, price-list name resolution, behaviour during a Google
outage, behaviour when the bot is refused permission to post, restart recovery,
the spreadsheet cell mapping, the arguments actually sent to the sheet, and a
randomised full-draft simulation that checks the invariants that matter —
nobody owns the same Pokémon twice, budgets never go negative, spent +
remaining always equals the starting budget, and pick order has no gaps.

The roster and Tera cell mapping has also been verified against the real
template: all 480 cells land in the right coach's block, every tick box is a
genuine check box, and the image and cost formulas either side are never
touched.

## Troubleshooting

**Slash commands do not appear.** Global registration can take up to an hour. Set
`DEV_GUILD_ID` in `.env` to your server ID and restart for instant registration.
Also check the bot was invited with the `applications.commands` scope.

**"There is no draft in this channel."** Most commands see only the pool in the
channel they were typed in. Run them in that pool's channel, or use the `name:`
option where one is offered. Remember that `/setup` is run in the **admin**
channel and names the draft channels, not the other way round.

**"This command is for draft moderators."** Nobody can run the moderator
commands until somebody with the **Administrator** permission has run
`/draft-moderator role:@Your Role`.

**The bot cannot post** (`403 Forbidden`, `Missing Access`). Right-click the
channel → **Edit Channel** → **Permissions**, add the bot or its role, and allow
**View Channel** and **Send Messages**. Do the same for the admin channel, since
trades and substitution requests are posted there. Error code 50001 means the bot cannot even see the
channel; 50013 means it can see it but not post. While this is unresolved the
turn clock stays paused, so nobody is skipped — fix it and restart the bot.

**A pick worked but the sheet did not update.** That is by design — the pick is
already saved locally. Check `journalctl -u draftbot -f` for the Google error,
then run `/sync` once it is fixed.

**Nothing happens when a timer runs out.** Check the bot is actually running
(`sudo systemctl status draftbot`). Timers only run while the process is alive;
after a restart the current player's clock is restarted rather than skipped.
