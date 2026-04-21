import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import math
from datetime import datetime, timezone

TOKEN     = os.getenv('DISCORD_TOKEN')
DATA_FILE = 'matchmaking.json'

# ── Rank tiers ────────────────────────────────────────────────────────────────
RANKS = [
    (0,    'Bronze',   '🥉', discord.Colour.from_rgb(205, 127, 50)),
    (600,  'Silver',   '🥈', discord.Colour.from_rgb(192, 192, 192)),
    (800,  'Gold',     '🥇', discord.Colour.from_rgb(255, 215, 0)),
    (1000, 'Platinum', '💎', discord.Colour.from_rgb(100, 200, 255)),
    (1200, 'Diamond',  '💠', discord.Colour.from_rgb(180, 100, 255)),
]

def get_rank(elo):
    rank = RANKS[0]
    for threshold, name, emoji, colour in RANKS:
        if elo >= threshold:
            rank = (threshold, name, emoji, colour)
    return rank

# ── Regions / ping buckets ────────────────────────────────────────────────────
REGIONS = ['EU', 'NA', 'SA', 'AS', 'OCE']

# ── ELO calculation ───────────────────────────────────────────────────────────
K = 32

def expected_score(a, b):
    return 1 / (1 + 10 ** ((b - a) / 400))

def new_elos(winner_elo, loser_elo):
    e = expected_score(winner_elo, loser_elo)
    gained = round(K * (1 - e))
    lost   = round(K * e)
    gained = max(10, gained)
    lost   = max(5,  lost)
    return winner_elo + gained, loser_elo - lost, gained, lost

# ── Data helpers ──────────────────────────────────────────────────────────────

def load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {}

def save(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def guild_data(gid):
    d = load()
    if gid not in d:
        d[gid] = {'players': {}, 'matches': [], 'queue': [], 'settings': {}, 'match_counter': 0}
        save(d)
    return d[gid]

def save_guild(gid, gdata):
    d = load()
    d[gid] = gdata
    save(d)

def get_player(gdata, uid):
    if uid not in gdata['players']:
        return None
    return gdata['players'][uid]

def default_player(uid, name):
    return {
        'uid': uid, 'name': name, 'elo': 500,
        'wins': 0, 'losses': 0, 'kills': 0, 'deaths': 0,
        'matches': [], 'registered_at': datetime.now(timezone.utc).isoformat()
    }

# ── intents ───────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ── Matchmaking loop ──────────────────────────────────────────────────────────

@tasks.loop(seconds=15)
async def matchmaking_loop():
    all_data = load()
    for gid, gdata in all_data.items():
        queue = gdata.get('queue', [])
        if len(queue) < 2:
            continue

        settings = gdata.get('settings', {})
        match_ch_id = settings.get('queue_channel_id')
        if not match_ch_id:
            continue

        matched = None
        # Try to find a same-region pair sorted by ELO proximity
        for i in range(len(queue)):
            for j in range(i + 1, len(queue)):
                p1 = queue[i]
                p2 = queue[j]
                if p1['region'] == p2['region']:
                    matched = (i, j)
                    break
            if matched:
                break

        # Fallback: any pair closest by ELO
        if not matched:
            best_diff = float('inf')
            for i in range(len(queue)):
                for j in range(i + 1, len(queue)):
                    diff = abs(queue[i]['elo'] - queue[j]['elo'])
                    if diff < best_diff:
                        best_diff = diff
                        matched = (i, j)

        if not matched:
            continue

        i, j = matched
        p1_q = queue[i]
        p2_q = queue[j]

        # Remove from queue (higher index first)
        for idx in sorted([i, j], reverse=True):
            queue.pop(idx)
        gdata['queue'] = queue

        # Create match
        gdata['match_counter'] = gdata.get('match_counter', 0) + 1
        match_id = gdata['match_counter']

        match = {
            'id': match_id,
            'p1': p1_q['uid'],
            'p2': p2_q['uid'],
            'p1_name': p1_q['name'],
            'p2_name': p2_q['name'],
            'p1_elo': p1_q['elo'],
            'p2_elo': p2_q['elo'],
            'region': p1_q['region'],
            'status': 'ongoing',
            'winner': None,
            'result_screenshot': None,
            'p1_score': 0,
            'p2_score': 0,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'thread_id': None,
        }

        save_guild(gid, gdata)

        try:
            channel = await bot.fetch_channel(int(match_ch_id))
            guild   = bot.get_guild(int(gid))

            # Create private thread
            thread = await channel.create_thread(
                name=f"Match #{match_id} | {p1_q['name']} vs {p2_q['name']}",
                type=discord.ChannelType.private_thread,
                invitable=False,
            )

            # Add both players
            try:
                m1 = guild.get_member(int(p1_q['uid'])) or await guild.fetch_member(int(p1_q['uid']))
                m2 = guild.get_member(int(p2_q['uid'])) or await guild.fetch_member(int(p2_q['uid']))
                await thread.add_user(m1)
                await thread.add_user(m2)
            except Exception as e:
                print(f"[Match] Could not add members: {e}")

            # Also add refs
            ref_role_name = settings.get('ref_role', 'Ref')
            for member in guild.members:
                for role in member.roles:
                    if role.name.lower() == ref_role_name.lower():
                        try: await thread.add_user(member)
                        except Exception: pass

            _, rank1_name, rank1_emoji, _ = get_rank(p1_q['elo'])
            _, rank2_name, rank2_emoji, _ = get_rank(p2_q['elo'])

            embed = discord.Embed(
                title=f"⚔️  Match #{match_id} — First to 5",
                description=(
                    f"**{p1_q['name']}** {rank1_emoji} {rank1_name} ({p1_q['elo']} ELO)\n"
                    f"vs\n"
                    f"**{p2_q['name']}** {rank2_emoji} {rank2_name} ({p2_q['elo']} ELO)\n\n"
                    f"🌍 Region: **{p1_q['region']}**\n\n"
                    f"📸 When the match is over, **one player must post a screenshot** of the final leaderboard.\n"
                    f"A ref will then confirm the result using `/confirm-result`."
                ),
                colour=discord.Colour.gold(),
            )
            embed.set_footer(text=f"Match ID: {match_id}")
            embed.timestamp = datetime.now(timezone.utc)

            await thread.send(embed=embed)

            # Store thread ID
            gdata2 = guild_data(gid)
            gdata2['matches'].append({**match, 'thread_id': str(thread.id)})
            save_guild(gid, gdata2)

        except Exception as e:
            print(f"[Matchmaking error] {e}")
            # Put players back in queue
            gdata2 = guild_data(gid)
            gdata2['queue'].append(p1_q)
            gdata2['queue'].append(p2_q)
            # Remove the match we just added
            gdata2['matches'] = [m for m in gdata2['matches'] if m['id'] != match_id]
            save_guild(gid, gdata2)

@matchmaking_loop.before_loop
async def before_loop(): await bot.wait_until_ready()

# ── Bot ready ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅  Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅  Synced {len(synced)} commands: {[c.name for c in synced]}")
    except Exception as e:
        print(f"❌  Sync failed: {e}")
    matchmaking_loop.start()
    print("✅  Matchmaking loop started")

# ── /register ─────────────────────────────────────────────────────────────────

@bot.tree.command(name="register", description="Register to play ranked 1v1s")
async def cmd_register(interaction: discord.Interaction):
    try:
        gid   = str(interaction.guild_id)
        uid   = str(interaction.user.id)
        gdata = guild_data(gid)
        if uid in gdata['players']:
            await interaction.response.send_message("❌  You're already registered!", ephemeral=True); return
        gdata['players'][uid] = default_player(uid, interaction.user.display_name)
        save_guild(gid, gdata)
        _, rank_name, rank_emoji, colour = get_rank(500)
        embed = discord.Embed(title="✅  Registered!", description=f"Welcome to ranked 1v1s, **{interaction.user.display_name}**!\n\nStarting ELO: **500** | Rank: {rank_emoji} **{rank_name}**", colour=colour)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /queue ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="queue", description="Join the matchmaking queue")
@app_commands.describe(region="Your region for ping matching")
@app_commands.choices(region=[app_commands.Choice(name=r, value=r) for r in REGIONS])
async def cmd_queue(interaction: discord.Interaction, region: str):
    try:
        gid   = str(interaction.guild_id)
        uid   = str(interaction.user.id)
        gdata = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message("❌  You need to `/register` first!", ephemeral=True); return
        if any(q['uid'] == uid for q in gdata['queue']):
            await interaction.response.send_message("❌  You're already in the queue!", ephemeral=True); return
        # Check not in ongoing match
        for m in gdata['matches']:
            if m['status'] == 'ongoing' and uid in (m['p1'], m['p2']):
                await interaction.response.send_message("❌  You're already in an ongoing match!", ephemeral=True); return
        gdata['queue'].append({'uid': uid, 'name': player['name'], 'elo': player['elo'], 'region': region, 'queued_at': datetime.now(timezone.utc).isoformat()})
        save_guild(gid, gdata)
        pos = len(gdata['queue'])
        _, rank_name, rank_emoji, _ = get_rank(player['elo'])
        await interaction.response.send_message(f"✅  Joined the **{region}** queue! Position: **#{pos}** | {rank_emoji} {rank_name} ({player['elo']} ELO)\n\nYou'll be notified when a match is found!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /leavequeue ───────────────────────────────────────────────────────────────

@bot.tree.command(name="leavequeue", description="Leave the matchmaking queue")
async def cmd_leavequeue(interaction: discord.Interaction):
    try:
        gid   = str(interaction.guild_id)
        uid   = str(interaction.user.id)
        gdata = guild_data(gid)
        before = len(gdata['queue'])
        gdata['queue'] = [q for q in gdata['queue'] if q['uid'] != uid]
        save_guild(gid, gdata)
        if len(gdata['queue']) < before:
            await interaction.response.send_message("✅  You've left the queue.", ephemeral=True)
        else:
            await interaction.response.send_message("❌  You're not in the queue.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /confirm-result ───────────────────────────────────────────────────────────

@bot.tree.command(name="confirm-result", description="Confirm a match result (Ref only)")
@app_commands.describe(match_id="The match ID to confirm", winner_id="Discord user ID of the winner", p1_score="Player 1 final score", p2_score="Player 2 final score")
async def cmd_confirm(interaction: discord.Interaction, match_id: int, winner_id: str, p1_score: int, p2_score: int):
    try:
        gid     = str(interaction.guild_id)
        gdata   = guild_data(gid)
        settings = gdata.get('settings', {})
        ref_role = settings.get('ref_role', 'Ref')

        is_ref = any(r.name.lower() == ref_role.lower() for r in interaction.user.roles)
        if not is_ref and not interaction.permissions.administrator:
            await interaction.response.send_message(f"❌  You need the **{ref_role}** role.", ephemeral=True); return

        match = next((m for m in gdata['matches'] if m['id'] == match_id), None)
        if not match:
            await interaction.response.send_message(f"❌  Match #{match_id} not found.", ephemeral=True); return
        if match['status'] != 'ongoing':
            await interaction.response.send_message(f"❌  Match #{match_id} is already {match['status']}.", ephemeral=True); return
        if winner_id not in (match['p1'], match['p2']):
            await interaction.response.send_message("❌  Winner ID must be one of the two players in this match.", ephemeral=True); return

        loser_id = match['p2'] if winner_id == match['p1'] else match['p1']
        w_player = gdata['players'].get(winner_id)
        l_player = gdata['players'].get(loser_id)
        if not w_player or not l_player:
            await interaction.response.send_message("❌  Player data not found.", ephemeral=True); return

        old_w_elo = w_player['elo']
        old_l_elo = l_player['elo']
        new_w_elo, new_l_elo, gained, lost = new_elos(old_w_elo, old_l_elo)

        w_player['elo']   = new_w_elo
        w_player['wins'] += 1
        w_player['kills'] += p1_score if winner_id == match['p1'] else p2_score
        w_player['deaths'] += p2_score if winner_id == match['p1'] else p1_score

        l_player['elo']     = max(100, new_l_elo)
        l_player['losses'] += 1
        l_player['kills']  += p2_score if winner_id == match['p1'] else p1_score
        l_player['deaths'] += p1_score if winner_id == match['p1'] else p2_score

        match['status']   = 'completed'
        match['winner']   = winner_id
        match['p1_score'] = p1_score
        match['p2_score'] = p2_score
        match['confirmed_by'] = str(interaction.user.id)
        match['completed_at'] = datetime.now(timezone.utc).isoformat()

        w_player['matches'].append(match_id)
        l_player['matches'].append(match_id)

        save_guild(gid, gdata)

        _, w_rank, w_emoji, w_colour = get_rank(new_w_elo)
        _, l_rank, l_emoji, _        = get_rank(new_l_elo)

        embed = discord.Embed(
            title=f"✅  Match #{match_id} — Result Confirmed",
            colour=w_colour,
        )
        embed.add_field(name="🏆  Winner", value=f"<@{winner_id}>\n{w_emoji} {w_rank} | {old_w_elo} → **{new_w_elo}** ELO (+{gained})", inline=True)
        embed.add_field(name="💀  Loser",  value=f"<@{loser_id}>\n{l_emoji} {l_rank} | {old_l_elo} → **{new_l_elo}** ELO (-{lost})", inline=True)
        embed.add_field(name="📊  Score",  value=f"{p1_score} — {p2_score}", inline=False)
        embed.set_footer(text=f"Confirmed by {interaction.user.display_name}")
        embed.timestamp = datetime.now(timezone.utc)

        await interaction.response.send_message(embed=embed)

        # Post in thread if it exists
        if match.get('thread_id'):
            try:
                thread = await bot.fetch_channel(int(match['thread_id']))
                await thread.send(embed=embed)
                await thread.edit(archived=True, locked=True)
            except Exception: pass

    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /profile ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="profile", description="View your player card or another player's")
@app_commands.describe(user="The player to look up (leave empty for yourself)")
async def cmd_profile(interaction: discord.Interaction, user: discord.Member = None):
    try:
        gid    = str(interaction.guild_id)
        target = user or interaction.user
        uid    = str(target.id)
        gdata  = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message(f"❌  **{target.display_name}** is not registered.", ephemeral=True); return

        _, rank_name, rank_emoji, colour = get_rank(player['elo'])
        total = player['wins'] + player['losses']
        wr    = round(player['wins'] / total * 100) if total else 0
        kda   = round(player['kills'] / max(1, player['deaths']), 2)

        embed = discord.Embed(title=f"{rank_emoji}  {player['name']}'s Player Card", colour=colour)
        embed.add_field(name="🏅  Rank",    value=f"{rank_emoji} **{rank_name}**", inline=True)
        embed.add_field(name="📊  ELO",     value=f"**{player['elo']}**",          inline=True)
        embed.add_field(name="🎮  Matches", value=f"**{total}**",                  inline=True)
        embed.add_field(name="✅  Wins",    value=f"**{player['wins']}**",          inline=True)
        embed.add_field(name="❌  Losses",  value=f"**{player['losses']}**",        inline=True)
        embed.add_field(name="📈  Win Rate",value=f"**{wr}%**",                     inline=True)
        embed.add_field(name="🔫  Kills",   value=f"**{player['kills']}**",         inline=True)
        embed.add_field(name="💀  Deaths",  value=f"**{player['deaths']}**",        inline=True)
        embed.add_field(name="⚡  KDA",     value=f"**{kda}**",                     inline=True)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"Registered • {player['registered_at'][:10]}")

        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /leaderboard ──────────────────────────────────────────────────────────────

@bot.tree.command(name="leaderboard", description="View the top 10 ranked players")
async def cmd_leaderboard(interaction: discord.Interaction):
    try:
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        players = [(uid, p) for uid, p in gdata['players'].items() if p['wins'] + p['losses'] > 0]
        players.sort(key=lambda x: x[1]['elo'], reverse=True)
        top = players[:10]

        if not top:
            await interaction.response.send_message("No ranked players yet!", ephemeral=True); return

        embed = discord.Embed(title="🏆  Leaderboard — Top 10", colour=discord.Colour.gold())
        medals = ['🥇', '🥈', '🥉']
        lines = []
        for i, (uid, p) in enumerate(top):
            _, rank_name, rank_emoji, _ = get_rank(p['elo'])
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            lines.append(f"{medal} **{p['name']}** — {rank_emoji} {rank_name} | **{p['elo']}** ELO | {p['wins']}W {p['losses']}L")
        embed.description = "\n".join(lines)
        embed.timestamp = datetime.now(timezone.utc)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /match-history ────────────────────────────────────────────────────────────

@bot.tree.command(name="match-history", description="View recent match history for a player")
@app_commands.describe(user="Player to look up (leave empty for yourself)")
async def cmd_history(interaction: discord.Interaction, user: discord.Member = None):
    try:
        gid    = str(interaction.guild_id)
        target = user or interaction.user
        uid    = str(target.id)
        gdata  = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message(f"❌  **{target.display_name}** is not registered.", ephemeral=True); return

        matches = [m for m in gdata['matches'] if m['status'] == 'completed' and uid in (m['p1'], m['p2'])]
        matches.sort(key=lambda x: x.get('completed_at', ''), reverse=True)
        recent = matches[:10]

        if not recent:
            await interaction.response.send_message(f"**{target.display_name}** has no completed matches yet.", ephemeral=True); return

        embed = discord.Embed(title=f"📋  Match History — {target.display_name}", colour=discord.Colour.blurple())
        lines = []
        for m in recent:
            won  = m['winner'] == uid
            opp  = m['p2_name'] if uid == m['p1'] else m['p1_name']
            my_s = m['p1_score'] if uid == m['p1'] else m['p2_score']
            op_s = m['p2_score'] if uid == m['p1'] else m['p1_score']
            result = "✅ W" if won else "❌ L"
            date = m.get('completed_at', '')[:10]
            lines.append(f"{result} | **#{m['id']}** vs **{opp}** | {my_s}–{op_s} | {date}")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /queue-status ─────────────────────────────────────────────────────────────

@bot.tree.command(name="queue-status", description="See who's currently in the queue")
async def cmd_queue_status(interaction: discord.Interaction):
    try:
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        queue = gdata.get('queue', [])
        if not queue:
            await interaction.response.send_message("The queue is currently empty.", ephemeral=True); return
        embed = discord.Embed(title=f"🎮  Queue — {len(queue)} player(s)", colour=discord.Colour.green())
        lines = []
        for i, q in enumerate(queue):
            _, rank_name, rank_emoji, _ = get_rank(q['elo'])
            lines.append(f"`#{i+1}` **{q['name']}** | {rank_emoji} {rank_name} ({q['elo']} ELO) | 🌍 {q['region']}")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /active-matches ───────────────────────────────────────────────────────────

@bot.tree.command(name="active-matches", description="See all currently ongoing matches")
async def cmd_active(interaction: discord.Interaction):
    try:
        gid     = str(interaction.guild_id)
        gdata   = guild_data(gid)
        ongoing = [m for m in gdata['matches'] if m['status'] == 'ongoing']
        if not ongoing:
            await interaction.response.send_message("No active matches right now.", ephemeral=True); return
        embed = discord.Embed(title=f"⚔️  Active Matches — {len(ongoing)}", colour=discord.Colour.orange())
        lines = []
        for m in ongoing:
            lines.append(f"**#{m['id']}** | {m['p1_name']} vs {m['p2_name']} | 🌍 {m['region']}")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /setup-queue ──────────────────────────────────────────────────────────────

@bot.tree.command(name="setup-queue", description="Set the channel where match threads are created — run IN the channel (Admin only)")
async def cmd_setup_queue(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['queue_channel_id'] = str(interaction.channel_id)
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Match threads will be created in <#{interaction.channel_id}>!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /setup-ref-role ───────────────────────────────────────────────────────────

@bot.tree.command(name="setup-ref-role", description="Set the role name for referees (Admin only)")
@app_commands.describe(role_name="Exact name of the ref role in your server")
async def cmd_setup_ref(interaction: discord.Interaction, role_name: str):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['ref_role'] = role_name
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Ref role set to **{role_name}**!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /reset-elo (Admin) ────────────────────────────────────────────────────────

@bot.tree.command(name="reset-elo", description="Reset a player's ELO to 500 (Admin only)")
@app_commands.describe(user="The player to reset")
async def cmd_reset_elo(interaction: discord.Interaction, user: discord.Member):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid   = str(interaction.guild_id)
        uid   = str(user.id)
        gdata = guild_data(gid)
        if uid not in gdata['players']:
            await interaction.response.send_message("❌  Player not registered.", ephemeral=True); return
        gdata['players'][uid]['elo'] = 500
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Reset **{user.display_name}**'s ELO to 500.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── /unregister (Admin) ───────────────────────────────────────────────────────

@bot.tree.command(name="unregister", description="Remove a player from the system (Admin only)")
@app_commands.describe(user="The player to remove")
async def cmd_unregister(interaction: discord.Interaction, user: discord.Member):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid   = str(interaction.guild_id)
        uid   = str(user.id)
        gdata = guild_data(gid)
        if uid not in gdata['players']:
            await interaction.response.send_message("❌  Player not registered.", ephemeral=True); return
        del gdata['players'][uid]
        gdata['queue'] = [q for q in gdata['queue'] if q['uid'] != uid]
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  **{user.display_name}** has been removed.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if not TOKEN: raise ValueError("DISCORD_TOKEN not set!")
    bot.run(TOKEN)
