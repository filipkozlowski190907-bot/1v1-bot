import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import re
from datetime import datetime, timezone

TOKEN     = os.getenv('DISCORD_TOKEN')
DATA_FILE = 'matchmaking.json'

RANKS = [
    (0,    'Bronze',   '🥉', discord.Colour.from_rgb(205, 127, 50)),
    (600,  'Silver',   '🥈', discord.Colour.from_rgb(192, 192, 192)),
    (800,  'Gold',     '🥇', discord.Colour.from_rgb(255, 215, 0)),
    (1000, 'Platinum', '💎', discord.Colour.from_rgb(100, 200, 255)),
    (1200, 'Diamond',  '💠', discord.Colour.from_rgb(180, 100, 255)),
]

REGIONS = ['EU', 'NA', 'SA', 'AS', 'OCE']

BANNER_NAMES = [
    'Twilight Trio',
    'Legacy',
    'Sky Diver',
    'Kingdom Hearts II',
    'Beach Day',
    'Clock Tower',
]

K = 32

def get_rank(elo):
    rank = RANKS[0]
    for r in RANKS:
        if elo >= r[0]:
            rank = r
    return rank

def expected_score(a, b):
    return 1 / (1 + 10 ** ((b - a) / 400))

def new_elos(winner_elo, loser_elo):
    e = expected_score(winner_elo, loser_elo)
    gained = max(10, round(K * (1 - e)))
    lost   = max(5,  round(K * e))
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
        d[gid] = {
            'players': {}, 'matches': [], 'queue': [],
            'pending_matches': [], 'settings': {},
            'match_counter': 0, 'active_refs': {},
        }
        save(d)
    return d[gid]

def save_guild(gid, gdata):
    d = load()
    d[gid] = gdata
    save(d)

def get_player(gdata, uid):
    return gdata['players'].get(uid)

def default_player(uid, name):
    return {
        'uid': uid, 'name': name, 'elo': 500,
        'wins': 0, 'losses': 0, 'kills': 0, 'deaths': 0,
        'matches': [], 'banner': -1,
        'registered_at': datetime.now(timezone.utc).isoformat()
    }

# ── intents ───────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members      = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ── Ref board ─────────────────────────────────────────────────────────────────

def build_ref_embed(gdata):
    pending     = gdata.get('pending_matches', [])
    active_refs = gdata.get('active_refs', {})

    embed = discord.Embed(
        title="🎮  Referee Board",
        description="Click your region to claim a pending match.\nA match won't start until a ref claims it.",
        colour=discord.Colour.from_rgb(255, 165, 0),
    )

    for region in REGIONS:
        region_pending = [m for m in pending if m['region'] == region and m['status'] == 'waiting_for_ref']
        if region_pending:
            m = region_pending[0]
            val = f"⚔️ **Match #{m['id']}** needs a ref\n**{m['p1_name']}** vs **{m['p2_name']}**\n*Click to claim!*"
        else:
            val = "*No pending matches*"
        embed.add_field(name=f"🌍  {region}", value=val, inline=True)

    embed.set_footer(text="Only players with the Ref role can claim matches")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


class RefBoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for region in REGIONS:
            btn = discord.ui.Button(
                label=f"Ref {region}",
                style=discord.ButtonStyle.primary,
                custom_id=f"ref_region_{region}",
            )
            btn.callback = self._make_callback(region)
            self.add_item(btn)

    def _make_callback(self, region):
        async def callback(interaction: discord.Interaction):
            gid   = str(interaction.guild_id)
            gdata = guild_data(gid)
            settings  = gdata.get('settings', {})
            ref_role  = settings.get('ref_role', 'Ref')

            is_ref = any(r.name.lower() == ref_role.lower() for r in interaction.user.roles)
            if not is_ref and not interaction.permissions.administrator:
                await interaction.response.send_message(f"❌  You need the **{ref_role}** role.", ephemeral=True)
                return

            uid = str(interaction.user.id)
            if uid in gdata.get('active_refs', {}):
                await interaction.response.send_message("❌  You're already assigned to a match!", ephemeral=True)
                return

            match = next((m for m in gdata.get('pending_matches', [])
                          if m['region'] == region and m['status'] == 'waiting_for_ref'), None)
            if not match:
                await interaction.response.send_message(f"❌  No pending matches in **{region}** right now.", ephemeral=True)
                return

            match['status'] = 'ref_claimed'
            match['ref_uid'] = uid
            gdata.setdefault('active_refs', {})[uid] = match['id']
            save_guild(gid, gdata)

            await interaction.response.send_message(
                f"✅  You've claimed **Match #{match['id']}** in **{region}**! Creating thread and VC...", ephemeral=True
            )
            await create_match(interaction.guild, gdata, match, uid)
            gdata2 = guild_data(gid)
            await update_ref_board(interaction.guild, gdata2, gdata2.get('settings', {}))
        return callback


async def update_ref_board(guild, gdata, settings):
    msg_id = settings.get('ref_message_id')
    ch_id  = settings.get('ref_channel_id')
    if not msg_id or not ch_id:
        return
    try:
        ch  = await bot.fetch_channel(int(ch_id))
        msg = await ch.fetch_message(int(msg_id))
        await msg.edit(embed=build_ref_embed(gdata), view=RefBoardView())
    except Exception as e:
        print(f"[RefBoard update error] {e}")


async def create_match(guild, gdata, pending, ref_uid):
    gid      = str(guild.id)
    settings = gdata.get('settings', {})
    ch_id    = settings.get('queue_channel_id')
    if not ch_id:
        return

    try:
        channel  = await bot.fetch_channel(int(ch_id))
        match_id = pending['id']
        p1_q     = pending['p1_data']
        p2_q     = pending['p2_data']

        # Private thread
        thread = await channel.create_thread(
            name=f"Match #{match_id} | {p1_q['name']} vs {p2_q['name']}",
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
        for uid in [p1_q['uid'], p2_q['uid'], ref_uid]:
            try:
                m = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                await thread.add_user(m)
            except Exception: pass

        # Private VC
        vc = None
        try:
            cat_id   = settings.get('vc_category_id')
            category = guild.get_channel(int(cat_id)) if cat_id else None
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False)
            }
            for uid in [p1_q['uid'], p2_q['uid'], ref_uid]:
                try:
                    m = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                    overwrites[m] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
                except Exception: pass
            vc = await guild.create_voice_channel(
                name=f"Match #{match_id} \u2014 {p1_q['name']} vs {p2_q['name']}",
                category=category,
                overwrites=overwrites,
            )
        except Exception as e:
            print(f"[VC create error] {e}")

        _, r1, e1, _ = get_rank(p1_q['elo'])
        _, r2, e2, _ = get_rank(p2_q['elo'])
        ref_m = guild.get_member(int(ref_uid)) or await guild.fetch_member(int(ref_uid))

        embed = discord.Embed(
            title=f"\u2694\ufe0f  Match #{match_id} \u2014 First to 5",
            description=(
                f"**{p1_q['name']}** {e1} {r1} ({p1_q['elo']} ELO)\nvs\n"
                f"**{p2_q['name']}** {e2} {r2} ({p2_q['elo']} ELO)\n\n"
                f"\U0001f30d Region: **{p1_q['region']}**\n"
                f"\U0001f46e Ref: **{ref_m.display_name}**\n"
                + (f"\U0001f3a4 VC: <#{vc.id}>\n" if vc else "") +
                "\n\U0001f4f8 When done, post a screenshot. Ref uses `/confirm-result` to confirm."
            ),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text=f"Match ID: {match_id}")
        embed.timestamp = datetime.now(timezone.utc)
        await thread.send(embed=embed)

        gdata2 = guild_data(gid)
        gdata2['matches'].append({
            'id': match_id, 'p1': p1_q['uid'], 'p2': p2_q['uid'],
            'p1_name': p1_q['name'], 'p2_name': p2_q['name'],
            'p1_elo': p1_q['elo'], 'p2_elo': p2_q['elo'],
            'region': p1_q['region'], 'ref_uid': ref_uid,
            'status': 'ongoing', 'winner': None,
            'p1_score': 0, 'p2_score': 0,
            'thread_id': str(thread.id),
            'vc_id': str(vc.id) if vc else None,
            'created_at': datetime.now(timezone.utc).isoformat(),
        })
        gdata2['pending_matches'] = [m for m in gdata2.get('pending_matches', []) if m['id'] != match_id]
        save_guild(gid, gdata2)

    except Exception as e:
        print(f"[create_match error] {e}")

# ── VC queue events ───────────────────────────────────────────────────────────

@bot.event
async def on_voice_state_update(member, before, after):
    gid   = str(member.guild.id)
    gdata = guild_data(gid)
    q_vcs = gdata.get('settings', {}).get('queue_vcs', {})
    uid   = str(member.id)

    # Left a queue VC
    if before.channel:
        for region, vc_id in q_vcs.items():
            if str(before.channel.id) == str(vc_id):
                in_match = any(
                    m['status'] == 'ongoing' and uid in (m['p1'], m['p2'])
                    for m in gdata.get('matches', [])
                )
                if not in_match:
                    gdata['queue'] = [q for q in gdata.get('queue', []) if q['uid'] != uid]
                    save_guild(gid, gdata)
                    try:
                        await member.send(f"\u274c  You left the **{region}** queue VC and were removed from the queue.")
                    except Exception: pass

    # Joined a queue VC
    if after.channel:
        for region, vc_id in q_vcs.items():
            if str(after.channel.id) == str(vc_id):
                player = get_player(gdata, uid)
                if not player:
                    try: await member.send("\u274c  You need to `/register` first!")
                    except Exception: pass
                    return
                if any(q['uid'] == uid for q in gdata.get('queue', [])):
                    try: await member.send("\u274c  You're already in the queue!")
                    except Exception: pass
                    return
                if any(m['status'] == 'ongoing' and uid in (m['p1'], m['p2']) for m in gdata.get('matches', [])):
                    try: await member.send("\u274c  You're already in an ongoing match!")
                    except Exception: pass
                    return

                gdata.setdefault('queue', []).append({
                    'uid': uid, 'name': player['name'],
                    'elo': player['elo'], 'region': region,
                    'queued_at': datetime.now(timezone.utc).isoformat()
                })
                save_guild(gid, gdata)

                _, rn, re_, _ = get_rank(player['elo'])
                try:
                    await member.send(
                        f"\u2705  Joined the **{region}** queue! {re_} {rn} ({player['elo']} ELO)\n"
                        f"Waiting for an opponent..."
                    )
                except Exception: pass

                await try_make_match(gid, gdata)
                break


async def try_make_match(gid, gdata):
    queue = gdata.get('queue', [])
    if len(queue) < 2:
        return

    matched = None
    for i in range(len(queue)):
        for j in range(i + 1, len(queue)):
            if queue[i]['region'] == queue[j]['region']:
                matched = (i, j); break
        if matched: break

    # Cross-region: if either player has been waiting 3+ min, match them with anyone
    if not matched:
        best = float('inf')
        for i in range(len(queue)):
            for j in range(i + 1, len(queue)):
                if queue[i].get('expanded') or queue[j].get('expanded'):
                    d = abs(queue[i]['elo'] - queue[j]['elo'])
                    if d < best:
                        best = d; matched = (i, j)

    # Last resort: closest ELO regardless of expansion
    if not matched:
        best = float('inf')
        for i in range(len(queue)):
            for j in range(i + 1, len(queue)):
                d = abs(queue[i]['elo'] - queue[j]['elo'])
                if d < best:
                    best = d; matched = (i, j)

    if not matched:
        return

    i, j = matched
    p1_q, p2_q = queue[i], queue[j]
    for idx in sorted([i, j], reverse=True):
        queue.pop(idx)
    gdata['queue'] = queue
    gdata['match_counter'] = gdata.get('match_counter', 0) + 1
    mid = gdata['match_counter']

    pending = {
        'id': mid, 'p1': p1_q['uid'], 'p2': p2_q['uid'],
        'p1_name': p1_q['name'], 'p2_name': p2_q['name'],
        'p1_elo': p1_q['elo'], 'p2_elo': p2_q['elo'],
        'region': p1_q['region'], 'status': 'waiting_for_ref',
        'p1_data': p1_q, 'p2_data': p2_q,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    gdata.setdefault('pending_matches', []).append(pending)
    save_guild(gid, gdata)

    guild = next((g for g in bot.guilds if str(g.id) == gid), None)
    if guild:
        for uid in [p1_q['uid'], p2_q['uid']]:
            opp = p2_q if uid == p1_q['uid'] else p1_q
            _, rn, re_, _ = get_rank(opp['elo'])
            try:
                m = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                await m.send(
                    f"\U0001f3ae  **Match found!** (Match #{mid})\n"
                    f"vs **{opp['name']}** {re_} {rn}\n"
                    f"Region: **{p1_q['region']}**\n\n"
                    f"\u23f3 Waiting for a ref to claim the match..."
                )
            except Exception: pass
        await update_ref_board(guild, gdata, gdata.get('settings', {}))

# ── on_ready ──────────────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def queue_expand_task():
    now = datetime.now(timezone.utc)
    all_data = load()
    for gid, gdata in all_data.items():
        queue = gdata.get('queue', [])
        changed = False
        for entry in queue:
            queued_at = datetime.fromisoformat(entry['queued_at'])
            waited = (now - queued_at).total_seconds()
            if waited >= 180 and not entry.get('expanded'):  # 3 minutes
                entry['expanded'] = True
                changed = True
                guild = next((g for g in bot.guilds if str(g.id) == gid), None)
                if guild:
                    try:
                        member = guild.get_member(int(entry['uid'])) or await guild.fetch_member(int(entry['uid']))
                        await member.send(
                            f"⏳  You've been in queue for **3 minutes** with no match in **{entry['region']}**.
"
                            f"Your search has been expanded to **all regions** to find you a match faster!"
                        )
                    except Exception: pass
        if changed:
            gdata['queue'] = queue
            all_data[gid] = gdata
    save(all_data)
    # Try to make matches for expanded players
    for gid, gdata in all_data.items():
        await try_make_match(gid, gdata)

@queue_expand_task.before_loop
async def before_expand(): await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"\u2705  Logged in as {bot.user}")
    bot.add_view(RefBoardView())
    try:
        synced = await bot.tree.sync()
        print(f"\u2705  Synced {len(synced)} commands: {[c.name for c in synced]}")
    except Exception as e:
        print(f"\u274c  Sync failed: {e}")

# ── /register ─────────────────────────────────────────────────────────────────

@bot.tree.command(name="register", description="Register to play ranked 1v1s")
async def cmd_register(interaction: discord.Interaction):
    try:
        gid, uid = str(interaction.guild_id), str(interaction.user.id)
        gdata = guild_data(gid)
        if uid in gdata['players']:
            await interaction.response.send_message("\u274c  You're already registered!", ephemeral=True); return
        gdata['players'][uid] = default_player(uid, interaction.user.display_name)
        save_guild(gid, gdata)
        _, rn, re_, colour = get_rank(500)
        embed = discord.Embed(
            title="\u2705  Registered!",
            description=f"Welcome, **{interaction.user.display_name}**!\n\nStarting ELO: **500** | Rank: {re_} **{rn}**\n\nJoin a region queue VC to find a match!",
            colour=colour
        )
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

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
            await interaction.response.send_message(f"\u274c  **{target.display_name}** is not registered.", ephemeral=True); return

        _, rank_name, rank_emoji, colour = get_rank(player['elo'])
        total = player['wins'] + player['losses']
        wr    = round(player['wins'] / total * 100) if total else 0
        kda   = round(player['kills'] / max(1, player['deaths']), 2)

        embed = discord.Embed(title=f"\u2694\ufe0f  {player['name']}'s Player Card", colour=colour)

        settings   = gdata.get('settings', {})
        banners    = settings.get('banners', [])
        banner_idx = player.get('banner', -1)
        if 0 <= banner_idx < len(banners) and banners[banner_idx]:
            embed.set_image(url=banners[banner_idx])

        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(
            name="\u2015\u2015\u2015  \U0001f3c5  RANK",
            value=f"### {rank_emoji}  {rank_name}\n**{player['elo']} ELO**",
            inline=True
        )
        embed.add_field(
            name="\u2015\u2015\u2015  \U0001f3ae  RECORD",
            value=f"### {player['wins']}W / {player['losses']}L\n**{total} matches  \u2022  {wr}% WR**",
            inline=True
        )
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        embed.add_field(
            name="\u2015\u2015\u2015  \U0001f52b  KILLS",
            value=f"### {player['kills']}",
            inline=True
        )
        embed.add_field(
            name="\u2015\u2015\u2015  \U0001f480  DEATHS",
            value=f"### {player['deaths']}",
            inline=True
        )
        embed.add_field(
            name="\u2015\u2015\u2015  \u26a1  KDA",
            value=f"### {kda}",
            inline=True
        )
        embed.set_footer(text=f"Registered  \u2022  {player['registered_at'][:10]}")
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

# ── /set-banner ───────────────────────────────────────────────────────────────

@bot.tree.command(name="set-banner", description="Choose your profile banner")
@app_commands.describe(banner="Which banner to use")
@app_commands.choices(banner=[app_commands.Choice(name=f"{i+1} - {n}", value=i) for i, n in enumerate(BANNER_NAMES)])
async def cmd_set_banner(interaction: discord.Interaction, banner: int):
    try:
        gid, uid = str(interaction.guild_id), str(interaction.user.id)
        gdata  = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message("\u274c  You need to `/register` first!", ephemeral=True); return
        banners = gdata.get('settings', {}).get('banners', [])
        if banner >= len(banners) or not banners[banner]:
            await interaction.response.send_message(f"\u274c  Banner **{BANNER_NAMES[banner]}** hasn't been set up yet.", ephemeral=True); return
        gdata['players'][uid]['banner'] = banner
        save_guild(gid, gdata)
        await interaction.response.send_message(f"\u2705  Banner set to **{BANNER_NAMES[banner]}**!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

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

        embed = discord.Embed(title="\U0001f3c6  Ranked Leaderboard  \u2014  Top 10", colour=discord.Colour.gold())
        medals = ['\U0001f947', '\U0001f948', '\U0001f949']
        lines  = []
        for i, (uid, p) in enumerate(top):
            _, rn, re_, _ = get_rank(p['elo'])
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            total = p['wins'] + p['losses']
            wr    = round(p['wins'] / total * 100) if total else 0
            lines.append(
                f"{medal}  **{p['name']}**\n"
                f"\u2523 {re_} {rn}  \u2022  **{p['elo']} ELO**\n"
                f"\u2517 {p['wins']}W  {p['losses']}L  \u2022  {wr}% WR\n"
            )

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Updated \u2022 {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC")
        embed.timestamp = datetime.now(timezone.utc)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

# ── /match-history ────────────────────────────────────────────────────────────

@bot.tree.command(name="match-history", description="View recent match history")
@app_commands.describe(user="Player to look up (leave empty for yourself)")
async def cmd_history(interaction: discord.Interaction, user: discord.Member = None):
    try:
        gid    = str(interaction.guild_id)
        target = user or interaction.user
        uid    = str(target.id)
        gdata  = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message(f"\u274c  **{target.display_name}** is not registered.", ephemeral=True); return

        matches = [m for m in gdata['matches'] if m['status'] == 'completed' and uid in (m['p1'], m['p2'])]
        matches.sort(key=lambda x: x.get('completed_at', ''), reverse=True)
        recent = matches[:10]

        if not recent:
            await interaction.response.send_message(f"**{target.display_name}** has no completed matches yet.", ephemeral=True); return

        embed = discord.Embed(title=f"\U0001f4cb  Match History \u2014 {target.display_name}", colour=discord.Colour.blurple())
        lines = []
        for m in recent:
            won  = m['winner'] == uid
            opp  = m['p2_name'] if uid == m['p1'] else m['p1_name']
            my_s = m['p1_score'] if uid == m['p1'] else m['p2_score']
            op_s = m['p2_score'] if uid == m['p1'] else m['p1_score']
            result = "\u2705 **W**" if won else "\u274c **L**"
            date   = m.get('completed_at', '')[:10]
            lines.append(f"{result}  \u2022  **#{m['id']}** vs **{opp}**  \u2022  `{my_s}\u20130{op_s}`  \u2022  {date}")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

# ── /queue-status ─────────────────────────────────────────────────────────────

@bot.tree.command(name="queue-status", description="See who's in the queue and pending matches")
async def cmd_queue_status(interaction: discord.Interaction):
    try:
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        queue   = gdata.get('queue', [])
        pending = gdata.get('pending_matches', [])

        embed = discord.Embed(title="\U0001f3ae  Queue Status", colour=discord.Colour.green())

        if queue:
            lines = []
            for i, q in enumerate(queue):
                _, rn, re_, _ = get_rank(q['elo'])
                lines.append(f"`#{i+1}`  **{q['name']}**  \u2022  {re_} {rn} ({q['elo']} ELO)  \u2022  \U0001f30d {q['region']}")
            embed.add_field(name=f"\u23f3  In Queue \u2014 {len(queue)}", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="\u23f3  In Queue", value="*Empty*", inline=False)

        if pending:
            lines = [f"**#{m['id']}**  {m['p1_name']} vs {m['p2_name']}  \u2022  \U0001f30d {m['region']}  \u2022  Waiting for ref" for m in pending]
            embed.add_field(name=f"\U0001f50d  Waiting for Ref \u2014 {len(pending)}", value="\n".join(lines), inline=False)

        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

# ── /active-matches ───────────────────────────────────────────────────────────

@bot.tree.command(name="active-matches", description="See all ongoing matches")
async def cmd_active(interaction: discord.Interaction):
    try:
        gid     = str(interaction.guild_id)
        gdata   = guild_data(gid)
        ongoing = [m for m in gdata['matches'] if m['status'] == 'ongoing']
        if not ongoing:
            await interaction.response.send_message("No active matches right now.", ephemeral=True); return
        embed = discord.Embed(title=f"\u2694\ufe0f  Active Matches \u2014 {len(ongoing)}", colour=discord.Colour.orange())
        lines = [f"**#{m['id']}**  \u2022  {m['p1_name']} vs {m['p2_name']}  \u2022  \U0001f30d {m['region']}" for m in ongoing]
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

# ── /confirm-result ───────────────────────────────────────────────────────────

@bot.tree.command(name="confirm-result", description="Confirm a match result (Ref only)")
@app_commands.describe(match_id="Match ID", winner_id="Discord user ID of the winner", p1_score="Player 1 score", p2_score="Player 2 score")
async def cmd_confirm(interaction: discord.Interaction, match_id: int, winner_id: str, p1_score: int, p2_score: int):
    try:
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        ref_role = gdata.get('settings', {}).get('ref_role', 'Ref')

        is_ref = any(r.name.lower() == ref_role.lower() for r in interaction.user.roles)
        if not is_ref and not interaction.permissions.administrator:
            await interaction.response.send_message(f"\u274c  You need the **{ref_role}** role.", ephemeral=True); return

        match = next((m for m in gdata['matches'] if m['id'] == match_id), None)
        if not match:
            await interaction.response.send_message(f"\u274c  Match #{match_id} not found.", ephemeral=True); return
        if match['status'] != 'ongoing':
            await interaction.response.send_message(f"\u274c  Match #{match_id} is already {match['status']}.", ephemeral=True); return
        if winner_id not in (match['p1'], match['p2']):
            await interaction.response.send_message("\u274c  Winner ID must be one of the two players.", ephemeral=True); return

        loser_id = match['p2'] if winner_id == match['p1'] else match['p1']
        w_p = gdata['players'].get(winner_id)
        l_p = gdata['players'].get(loser_id)
        if not w_p or not l_p:
            await interaction.response.send_message("\u274c  Player data not found.", ephemeral=True); return

        old_w, old_l = w_p['elo'], l_p['elo']
        new_w, new_l, gained, lost = new_elos(old_w, old_l)

        w_p['elo'] = new_w; w_p['wins'] += 1
        w_p['kills']  += p1_score if winner_id == match['p1'] else p2_score
        w_p['deaths'] += p2_score if winner_id == match['p1'] else p1_score
        l_p['elo'] = max(100, new_l); l_p['losses'] += 1
        l_p['kills']  += p2_score if winner_id == match['p1'] else p1_score
        l_p['deaths'] += p1_score if winner_id == match['p1'] else p2_score

        match.update({
            'status': 'completed', 'winner': winner_id,
            'p1_score': p1_score, 'p2_score': p2_score,
            'confirmed_by': str(interaction.user.id),
            'completed_at': datetime.now(timezone.utc).isoformat(),
            'elo_gained': gained, 'elo_lost': lost,
        })
        w_p['matches'].append(match_id)
        l_p['matches'].append(match_id)

        ref_uid = match.get('ref_uid')
        if ref_uid and ref_uid in gdata.get('active_refs', {}):
            del gdata['active_refs'][ref_uid]

        save_guild(gid, gdata)

        _, wr, we, wc = get_rank(new_w)
        _, lr, le, _  = get_rank(new_l)

        embed = discord.Embed(title=f"\u2705  Match #{match_id} \u2014 Result Confirmed", colour=wc)
        embed.add_field(name="\U0001f3c6  Winner", value=f"<@{winner_id}>\n{we} {wr}\n{old_w} \u2192 **{new_w}** ELO (+{gained})", inline=True)
        embed.add_field(name="\U0001f480  Loser",  value=f"<@{loser_id}>\n{le} {lr}\n{old_l} \u2192 **{new_l}** ELO (-{lost})", inline=True)
        embed.add_field(name="\U0001f4ca  Score",  value=f"**{p1_score} \u2014 {p2_score}**", inline=False)
        embed.set_footer(text=f"Confirmed by {interaction.user.display_name}")
        embed.timestamp = datetime.now(timezone.utc)
        await interaction.response.send_message(embed=embed)

        if match.get('thread_id'):
            try:
                t = await bot.fetch_channel(int(match['thread_id']))
                await t.send(embed=embed)
                await t.edit(archived=True, locked=True)
            except Exception: pass

        if match.get('vc_id'):
            try:
                vc = bot.get_channel(int(match['vc_id']))
                if vc: await vc.delete()
            except Exception: pass

    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

# ── /rollback ─────────────────────────────────────────────────────────────────

@bot.tree.command(name="rollback", description="Roll back last N matches for a player (Admin only)")
@app_commands.describe(user="Player to roll back", games="Number of recent games to reverse")
async def cmd_rollback(interaction: discord.Interaction, user: discord.Member, games: int):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return

        gid, uid = str(interaction.guild_id), str(user.id)
        gdata  = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message("\u274c  Player not registered.", ephemeral=True); return

        completed = [m for m in gdata['matches'] if m['status'] == 'completed' and uid in (m['p1'], m['p2'])]
        completed.sort(key=lambda x: x.get('completed_at', ''), reverse=True)
        to_rb = completed[:games]

        if not to_rb:
            await interaction.response.send_message("\u274c  No completed matches to roll back.", ephemeral=True); return

        old_elo = player['elo']
        rolled  = []

        for m in to_rb:
            won = m['winner'] == uid
            if won:
                player['elo'] -= m.get('elo_gained', 20)
                player['wins'] = max(0, player['wins'] - 1)
            else:
                player['elo'] += m.get('elo_lost', 20)
                player['losses'] = max(0, player['losses'] - 1)
            player['elo'] = max(100, player['elo'])
            m['status'] = 'rolled_back'
            rolled.append(m['id'])

        save_guild(gid, gdata)

        embed = discord.Embed(title="\u21a9\ufe0f  Rollback Complete", colour=discord.Colour.orange())
        embed.add_field(name="Player",           value=f"<@{uid}>",                                            inline=True)
        embed.add_field(name="Games Rolled Back",value=f"**{len(rolled)}**  (#{', #'.join(str(x) for x in rolled)})", inline=True)
        embed.add_field(name="ELO Change",       value=f"{old_elo} \u2192 **{player['elo']}**",               inline=False)
        embed.set_footer(text=f"By {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

# ── /adjust-elo ───────────────────────────────────────────────────────────────

@bot.tree.command(name="adjust-elo", description="Add or remove ELO from a player (Admin only)")
@app_commands.describe(user="The player", amount="Amount e.g. 50 or -50")
async def cmd_adjust_elo(interaction: discord.Interaction, user: discord.Member, amount: int):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return

        gid, uid = str(interaction.guild_id), str(user.id)
        gdata  = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message("\u274c  Player not registered.", ephemeral=True); return

        old_elo = player['elo']
        player['elo'] = max(100, player['elo'] + amount)
        save_guild(gid, gdata)

        _, rn, re_, colour = get_rank(player['elo'])
        sign = "+" if amount >= 0 else ""
        embed = discord.Embed(
            title="\u2699\ufe0f  ELO Adjusted",
            description=f"<@{uid}>\n{old_elo} \u2192 **{player['elo']}** ELO  ({sign}{amount})\n{re_} {rn}",
            colour=colour
        )
        embed.set_footer(text=f"By {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

# ── Setup commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="setup-queue", description="Set channel where match threads are created — run IN the channel (Admin only)")
async def cmd_setup_queue(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['queue_channel_id'] = str(interaction.channel_id)
        save_guild(gid, gdata)
        await interaction.response.send_message(f"\u2705  Match threads will be created in <#{interaction.channel_id}>!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

@bot.tree.command(name="setup-ref-role", description="Set the referee role name (Admin only)")
@app_commands.describe(role_name="Exact name of the ref role")
async def cmd_setup_ref_role(interaction: discord.Interaction, role_name: str):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['ref_role'] = role_name
        save_guild(gid, gdata)
        await interaction.response.send_message(f"\u2705  Ref role set to **{role_name}**!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

@bot.tree.command(name="setup-region-vc", description="Set a queue VC for a region — join the VC first, then run this (Admin only)")
@app_commands.describe(region="Region for this VC")
@app_commands.choices(region=[app_commands.Choice(name=r, value=r) for r in REGIONS])
async def cmd_setup_region_vc(interaction: discord.Interaction, region: str):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("\u274c  Join the voice channel first, then run this command!", ephemeral=True); return
        vc_id = str(interaction.user.voice.channel.id)
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {}).setdefault('queue_vcs', {})[region] = vc_id
        save_guild(gid, gdata)
        await interaction.response.send_message(f"\u2705  **{interaction.user.voice.channel.name}** is now the **{region}** queue VC!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

@bot.tree.command(name="setup-vc-category", description="Set the category where match VCs are created — run in any channel in that category (Admin only)")
async def cmd_setup_vc_category(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return
        if not interaction.channel.category:
            await interaction.response.send_message("\u274c  This channel isn't in a category!", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['vc_category_id'] = str(interaction.channel.category.id)
        save_guild(gid, gdata)
        await interaction.response.send_message(f"\u2705  Match VCs will be created in the **{interaction.channel.category.name}** category!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

@bot.tree.command(name="post-ref-board", description="Post the ref availability board (Admin only)")
async def cmd_post_ref_board(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        msg   = await interaction.channel.send(embed=build_ref_embed(gdata), view=RefBoardView())
        gdata.setdefault('settings', {})['ref_message_id'] = str(msg.id)
        gdata['settings']['ref_channel_id'] = str(interaction.channel_id)
        save_guild(gid, gdata)
        await interaction.response.send_message("\u2705  Ref board posted!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

@bot.tree.command(name="setup-banner", description="Set a profile banner image URL (Admin only)")
@app_commands.describe(url="Direct image URL for this banner")
@app_commands.choices(slot=[app_commands.Choice(name=f"{i+1} - {n}", value=i+1) for i, n in enumerate(BANNER_NAMES)])
async def cmd_setup_banner(interaction: discord.Interaction, slot: int, url: str):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        banners = gdata.setdefault('settings', {}).setdefault('banners', [''] * 6)
        while len(banners) < 6:
            banners.append('')
        banners[slot - 1] = url
        gdata['settings']['banners'] = banners
        save_guild(gid, gdata)
        await interaction.response.send_message(f"\u2705  Banner **{BANNER_NAMES[slot-1]}** set!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

# ── Admin commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="reset-elo", description="Reset a player's ELO to 500 (Admin only)")
@app_commands.describe(user="The player to reset")
async def cmd_reset_elo(interaction: discord.Interaction, user: discord.Member):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return
        gid, uid = str(interaction.guild_id), str(user.id)
        gdata = guild_data(gid)
        if uid not in gdata['players']:
            await interaction.response.send_message("\u274c  Player not registered.", ephemeral=True); return
        gdata['players'][uid]['elo'] = 500
        save_guild(gid, gdata)
        await interaction.response.send_message(f"\u2705  Reset **{user.display_name}**'s ELO to 500.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

@bot.tree.command(name="unregister", description="Remove a player from the system (Admin only)")
@app_commands.describe(user="The player to remove")
async def cmd_unregister(interaction: discord.Interaction, user: discord.Member):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("\u274c  Admins only.", ephemeral=True); return
        gid, uid = str(interaction.guild_id), str(user.id)
        gdata = guild_data(gid)
        if uid not in gdata['players']:
            await interaction.response.send_message("\u274c  Player not registered.", ephemeral=True); return
        del gdata['players'][uid]
        gdata['queue'] = [q for q in gdata['queue'] if q['uid'] != uid]
        save_guild(gid, gdata)
        await interaction.response.send_message(f"\u2705  **{user.display_name}** has been removed.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"\u274c  {e}", ephemeral=True)

if __name__ == '__main__':
    if not TOKEN: raise ValueError("DISCORD_TOKEN not set!")
    bot.run(TOKEN)
