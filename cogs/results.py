"""
Reporting match results on Discord: each game's winner and each Pokémon's kills and deaths, by replay link or by
hand, and playing a match on Pokémon Champions (with each player's Pokepaste). The rules are in results.py;
draft.ogreclan.com offers the same on a league's Matches tab, and battles played there with Ready are recorded by
themselves.
"""

import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
import draft as rules
import results
import ui

log = logging.getLogger("draftbot.results")


async def read_replay(replay, discord_ids):
    """Asks draft.ogreclan.com to read a replay's kills and deaths, with its two players as Discord IDs"""
    site = config.get_site_api()
    if site is None:
        raise rules.DraftError("❌ Replay links can't be read right now. Report the game with `/reportgame` instead.")
    url, secret = site
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{url}/bot-api/replay-stats", json={"replay": replay, "players": discord_ids},
                                    headers={"X-OgreClan-Secret": secret}, timeout=aiohttp.ClientTimeout(total=20)) as response:
                body = await response.json(content_type=None)
    except Exception:
        log.exception("reading a replay from the site failed")
        raise rules.DraftError("❌ The site isn't answering right now. Try again, or use `/reportgame`.")
    if not body.get("ok"):
        raise rules.DraftError(f"❌ {body.get('error') or 'That replay could not be read.'}")
    return body


def describe(conn, league_id, view, players):
    """A match's report, for a reply"""
    name = {p["id"]: p["display_name"] for p in players}
    lines = []
    if view["champions"]:
        lines.append("🏆 Played on **Pokémon Champions**")
    for game in view["games"]:
        winner = name.get(game["winner_id"], "not given") if game["winner_id"] else "not given"
        lines.append(f"**Game {game['game']}** — winner: {winner}" + (f" ([replay]({game['replay']}))" if game["replay"] else ""))
        for player_id, mons in game["stats"].items():
            lines.append(f"• {name.get(player_id, 'Someone')}: " + ", ".join(f"{m} {k}-{d}" for m, k, d in mons))
    for player_id, url in view["pokepastes"].items():
        lines.append(f"📋 {name.get(player_id, 'Someone')}'s Pokepaste: {url}")
    if view["winner_id"]:
        lines.append(f"🏅 **{name.get(view['winner_id'], 'Someone')}** has won more games.")
    return "\n".join(lines) or "Nothing reported yet."


class ResultsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def league_for(self, interaction, name):
        """The league: by name, or the one this channel drafts"""
        if name:
            return rules.require_season_league_by_name(self.bot.conn, interaction.guild_id, name)
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        return rules.get_league(self.bot.conn, pool["league_id"])

    def your_match(self, interaction, week, name, user_id=None):
        league = self.league_for(interaction, name)
        player = results.player_in_league(self.bot.conn, league["id"], user_id or interaction.user.id)
        fixture = results.find_match(self.bot.conn, league["id"], week, player["id"])
        players = [rules.get_player_by_id(self.bot.conn, fixture["home_player_id"]),
                   rules.get_player_by_id(self.bot.conn, fixture["away_player_id"])]
        return league, player, fixture, players

    async def land_trades(self, league, week, fixture, players):
        """A finished match puts both players' approved trades into effect.

        Reported game by game, so this runs after each one and does nothing
        until the match is actually over. Returns a line for the reply.
        """
        applied = results.trades_after_match(self.bot.conn, league, week, fixture["match_number"])
        if not applied:
            return ""
        because = (f"**{players[0]['display_name']}** and **{players[1]['display_name']}** "
                   f"have finished their week {week} match")
        await ui.trades_landed(self.bot, applied, because)
        return (f"\n\n🔁 That finishes the match, so {len(applied)} approved trade(s) are "
                f"now in effect.")

    async def announce(self, league, players, text):
        """A short note in the players' pool channel (a league on Discord)"""
        if league["site_only"]:
            return
        pool = rules.get_pool(self.bot.conn, players[0]["pool_id"])
        await ui.safe_send(self.bot.get_channel(pool["channel_id"]), text)

    @app_commands.command(name="reportgame", description="Report a game of your match: your Pokémon's kills and deaths.")
    @app_commands.describe(week="The week of the match", game="Which game (1, 2, 3...)",
                           kd="Your Pokémon's kills-deaths, like: Garchomp 2-0, Rotom-Wash 1-1",
                           winner="Who won this game (either player can say)",
                           name="The league's name, if you're not in its draft channel")
    async def reportgame(self, interaction: discord.Interaction, week: int, game: int, kd: str,
                         winner: discord.Member = None, name: str = None):
        league, player, fixture, players = self.your_match(interaction, week, name)
        stats = results.parse_kd(kd, results.roster_of(self.bot.conn, player))
        winner_id = None
        if winner is not None:
            winner_player = next((p for p in players if p["user_id"] == winner.id), None)
            if winner_player is None:
                raise rules.DraftError("❌ The winner has to be one of the match's two players.")
            winner_id = winner_player["id"]
        view = results.record_game(self.bot.conn, league["id"], week, fixture["match_number"], game, "manual",
                                   winner_id=winner_id, stats={player["id"]: stats}, only_player=player["id"])
        landed = await self.land_trades(league, week, fixture, players)
        await interaction.response.send_message(
            f"✅ Game {game} of your week {week} match is reported.\n\n"
            f"{describe(self.bot.conn, league['id'], view, players)}{landed}",
            ephemeral=True)

    @app_commands.command(name="reportreplay", description="Report a game of your match with its replay link (both sides).")
    @app_commands.describe(week="The week of the match", game="Which game (1, 2, 3...)",
                           replay="The game's replay link on replay.ogreclan.com",
                           name="The league's name, if you're not in its draft channel")
    async def reportreplay(self, interaction: discord.Interaction, week: int, game: int, replay: str, name: str = None):
        league, player, fixture, players = self.your_match(interaction, week, name)
        await interaction.response.defer(ephemeral=True)
        body = await read_replay(replay, [str(p["user_id"]) for p in players])
        view = record_replay(self.bot.conn, league["id"], week, fixture["match_number"], game, players, body)
        landed = await self.land_trades(league, week, fixture, players)
        await interaction.followup.send(
            f"✅ Game {game} of your week {week} match is reported from its replay.\n\n"
            f"{describe(self.bot.conn, league['id'], view, players)}{landed}", ephemeral=True)

    @app_commands.command(name="champions", description="Agree to play your match on Pokémon Champions instead.")
    @app_commands.describe(week="The week of the match", agree="True to agree, False to take it back",
                           name="The league's name, if you're not in its draft channel")
    async def champions(self, interaction: discord.Interaction, week: int, agree: bool = True, name: str = None):
        league, player, fixture, players = self.your_match(interaction, week, name)
        view = results.ask_champions(self.bot.conn, league["id"], week, fixture["match_number"], player["id"], agree)
        opponent = next(p for p in players if p["id"] != player["id"])
        if view["champions"]:
            text = (f"🏆 Both players agreed: the week {week} match between **{players[0]['display_name']}** and "
                    f"**{players[1]['display_name']}** is played on Pokémon Champions. Each of you: `/pokepaste` your "
                    f"team and `/reportgame` your Pokémon's kills and deaths for each game.")
            await self.announce(league, players, text)
        elif agree:
            text = f"You've asked to play on Pokémon Champions. It's on once {opponent['display_name']} agrees too (`/champions week:{week}`)."
        else:
            text = "You've taken back playing on Champions."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="pokepaste", description="Give your team for a match played on Pokémon Champions.")
    @app_commands.describe(week="The week of the match", url="Your Pokepaste link (pokepast.es)",
                           name="The league's name, if you're not in its draft channel")
    async def pokepaste(self, interaction: discord.Interaction, week: int, url: str, name: str = None):
        league, player, fixture, players = self.your_match(interaction, week, name)
        results.set_pokepaste(self.bot.conn, league["id"], week, fixture["match_number"], player["id"], url)
        await interaction.response.send_message(f"✅ Your Pokepaste for week {week} is saved.", ephemeral=True)

    @app_commands.command(name="matchresult", description="See what's been reported for a match.")
    @app_commands.describe(week="The week", player="Whose match (yours if you leave it out)",
                           name="The league's name, if you're not in its draft channel")
    async def matchresult(self, interaction: discord.Interaction, week: int, player: discord.Member = None, name: str = None):
        league, _, fixture, players = self.your_match(interaction, week, name, player.id if player else None)
        view = results.match_view(self.bot.conn, league["id"], week, fixture["match_number"])
        await interaction.response.send_message(
            f"**{league['name']} — week {week}: {players[0]['display_name']} vs {players[1]['display_name']}**\n"
            f"{describe(self.bot.conn, league['id'], view, players)}", ephemeral=True)

    @app_commands.command(name="cleargame", description="Moderator only: remove a game's reported result.")
    @app_commands.describe(week="The week", game="Which game", player="One of the match's players",
                           name="The league's name, if this isn't its draft channel")
    async def cleargame(self, interaction: discord.Interaction, week: int, game: int, player: discord.Member,
                        name: str = None):
        ui.require_moderator(self.bot, interaction)
        league, _, fixture, players = self.your_match(interaction, week, name, player.id)
        results.clear_game(self.bot.conn, league["id"], week, fixture["match_number"], game)
        await interaction.response.send_message(f"🧹 Game {game} of that week {week} match is cleared.", ephemeral=True)


def record_replay(conn, league_id, week, match_number, game, players, body):
    """Record a game from what the site read from its replay: both sides, the winner and the link"""
    by_discord = {str(p["user_id"]): p for p in players}
    stats = {}
    for entry in body.get("pokemon") or []:
        player = by_discord.get(str(entry.get("discord_id")))
        if player is None:
            continue
        name = results.roster_name(entry["species"], results.roster_of(conn, player))
        stats.setdefault(player["id"], []).append((name, int(entry["kills"]), int(entry["deaths"])))
    if not stats:
        raise rules.DraftError("❌ That replay isn't between this match's players (or their Showdown accounts "
                               "aren't linked on draft.ogreclan.com).")
    winner = by_discord.get(str(body.get("winner") or ""))
    return results.record_game(conn, league_id, week, match_number, game, "replay",
                               winner_id=winner["id"] if winner else None, stats=stats, replay=body.get("replay"))


async def setup(bot):
    await bot.add_cog(ResultsCog(bot))
