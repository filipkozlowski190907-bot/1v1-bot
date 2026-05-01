import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import io
import aiohttp
from datetime import datetime, timezone

TOKEN     = os.getenv('DISCORD_TOKEN')
DATA_FILE = 'matchmaking.json'   # local cache — rebuilt from Discord on every startup

# ─── Discord-backed persistence ───────────────────────────────────────────────
# The bot uploads the full JSON to a private Discord channel after every save.
# On startup it downloads it back, so data survives redeployments completely.
#
# Setup: run /setup-data-channel inside a private channel once.
# Optionally set DATA_CHANNEL_ID as an env var to skip the command.
# ─────────────────────────────────────────────────────────────────────────────
_DATA_CHANNEL_ID = os.getenv('DATA_CHANNEL_ID', '')   # optional env-var shortcut

RANKS = [
    (0,    'Bronze',   '🥉', discord.Colour.from_rgb(205, 127, 50)),
    (600,  'Silver',   '🥈', discord.Colour.from_rgb(192, 192, 192)),
    (800,  'Gold',     '🥇', discord.Colour.from_rgb(255, 215, 0)),
    (1000, 'Platinum', '💎', discord.Colour.from_rgb(100, 200, 255)),
    (1200, 'Diamond',  '💠', discord.Colour.from_rgb(180, 100, 255)),
]
REGIONS      = ['EU', 'NA', 'SA', 'AS', 'OCE']
BANNER_NAMES = ['Twilight Trio', 'Legacy', 'Sky Diver', 'Kingdom Hearts II', 'Beach Day', 'Clock Tower']
K = 32

def get_rank(elo):
    rank = RANKS[0]
    for r in RANKS:
        if elo >= r[0]: rank = r
    return rank

def expected_score(a, b): return 1 / (1 + 10 ** ((b - a) / 400))

def new_elos(winner_elo, loser_elo):
    e = expected_score(winner_elo, loser_elo)
    return winner_elo + max(10, round(K*(1-e))), loser_elo - max(5, round(K*e)), max(10, round(K*(1-e))), max(5, round(K*e))

def load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f: return json.load(f)
    return {}

def save(data):
    tmp = DATA_FILE + '.tmp'
    with open(tmp, 'w') as f: json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)

def _get_data_channel_id():
    """Return the data-channel ID from env var or any guild's settings."""
    if _DATA_CHANNEL_ID: return _DATA_CHANNEL_ID
    try:
        for gdata in load().values():
            cid = gdata.get('settings', {}).get('data_channel_id')
            if cid: return cid
    except Exception: pass
    return None

async def push_to_discord():
    """Upload the current JSON to the data channel so it survives redeploys."""
    ch_id = _get_data_channel_id()
    if not ch_id: return
    try:
        ch      = await bot.fetch_channel(int(ch_id))
        content = json.dumps(load(), indent=2).encode()
        file    = discord.File(io.BytesIO(content), filename='matchmaking.json')
        # Edit the last bot message if there is one, otherwise post a new one
        async for msg in ch.history(limit=20):
            if msg.author == bot.user and msg.attachments:
                await msg.edit(content="📦 **Bot data — do not delete**", attachments=[file])
                return
        await ch.send(content="📦 **Bot data — do not delete**", file=file)
    except Exception as e: print(f"[Discord persist push error] {e}")

async def pull_from_discord():
    """On startup: fetch the JSON from the data channel and restore it locally."""
    ch_id = _get_data_channel_id()
    if not ch_id: return False
    try:
        ch = await bot.fetch_channel(int(ch_id))
        async for msg in ch.history(limit=20):
            if msg.author == bot.user:
                for att in msg.attachments:
                    if att.filename == 'matchmaking.json':
                        async with aiohttp.ClientSession() as session:
                            async with session.get(att.url) as resp:
                                text = await resp.text()
                        data = json.loads(text)
                        save(data)
                        print(f"[Discord persist] Restored {sum(len(g.get('players',{})) for g in data.values())} players from Discord.")
                        return True
    except Exception as e: print(f"[Discord persist pull error] {e}")
    return False

def save_and_push(data):
    """Save locally then schedule a Discord push (fire-and-forget)."""
    save(data)
    try:
        loop = bot.loop
        if loop and loop.is_running():
            loop.create_task(push_to_discord())
    except Exception: pass

def guild_data(gid):
    d = load()
    if gid not in d:
        d[gid] = {'players': {}, 'matches': [], 'queue': [], 'pending_matches': [], 'settings': {}, 'match_counter': 0, 'active_refs': {}}
        save_and_push(d)
    return d[gid]

def save_guild(gid, gdata):
    d = load(); d[gid] = gdata; save_and_push(d)

def get_player(gdata, uid): return gdata['players'].get(uid)

def default_player(uid, name):
    return {'uid': uid, 'name': name, 'elo': 500, 'wins': 0, 'losses': 0, 'kills': 0, 'deaths': 0, 'matches': [], 'banner': -1, 'registered_at': datetime.now(timezone.utc).isoformat()}

def match_score(p1, p2):
    elo_diff  = abs(p1['elo'] - p2['elo'])
    kda1 = p1['kills'] / max(1, p1['deaths'])
    kda2 = p2['kills'] / max(1, p2['deaths'])
    kda_diff  = abs(kda1 - kda2)
    region_bonus = 0 if p1['region'] == p2['region'] else 200
    return elo_diff + kda_diff * 50 + region_bonus

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

def build_ref_embed(gdata):
    pending = gdata.get('pending_matches', [])
    embed = discord.Embed(title="🎮  Referee Board", description="Click your region to claim a pending match.\nA match won't start until a ref claims it.", colour=discord.Colour.from_rgb(255, 165, 0))
    for region in REGIONS:
        rp = [m for m in pending if m['region'] == region and m['status'] == 'waiting_for_ref']
        count = len(rp)
        if count == 1:   val = "⚔️  **1 game** needs a ref\n*Click to claim!*"
        elif count > 1:  val = f"⚔️  **{count} games** need a ref\n*Click to claim!*"
        else:            val = "*No pending matches*"
        embed.add_field(name=f"🌍  {region}", value=val, inline=True)
    embed.set_footer(text="Only players with the Ref role can claim matches")
    embed.timestamp = datetime.now(timezone.utc)
    return embed

class RefBoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for region in REGIONS:
            btn = discord.ui.Button(label=f"Ref {region}", style=discord.ButtonStyle.primary, custom_id=f"ref_region_{region}")
            btn.callback = self._make_callback(region)
            self.add_item(btn)

    def _make_callback(self, region):
        async def callback(interaction: discord.Interaction):
            gid = str(interaction.guild_id)
            gdata = guild_data(gid)
            ref_role = gdata.get('settings', {}).get('ref_role', 'Ref')
            is_ref = any(r.name.lower() == ref_role.lower() for r in interaction.user.roles)
            if not is_ref and not interaction.permissions.administrator:
                await interaction.response.send_message(f"❌  You need the **{ref_role}** role.", ephemeral=True); return
            uid = str(interaction.user.id)
            if uid in gdata.get('active_refs', {}):
                await interaction.response.send_message("❌  You're already assigned to a match!", ephemeral=True); return
            match = next((m for m in gdata.get('pending_matches', []) if m['region'] == region and m['status'] == 'waiting_for_ref'), None)
            if not match:
                await interaction.response.send_message(f"❌  No pending matches in **{region}** right now.", ephemeral=True); return
            match['status'] = 'ref_claimed'
            match['ref_uid'] = uid
            gdata.setdefault('active_refs', {})[uid] = match['id']
            save_guild(gid, gdata)
            thread, vc = await create_match(interaction.guild, gdata, match, uid)
            vc_mention = f"<#{vc.id}>" if vc else "N/A"
            thread_mention = f"<#{thread.id}>" if thread else "N/A"
            await interaction.response.send_message(
                f"✅  You've claimed **Match #{match['id']}** in **{region}**!\n"
                f"📝 Thread: {thread_mention}\n"
                f"🎤 VC: {vc_mention}",
                ephemeral=True
            )
            gdata2 = guild_data(gid)
            await update_ref_board(interaction.guild, gdata2, gdata2.get('settings', {}))
        return callback

async def update_ref_board(guild, gdata, settings):
    msg_id = settings.get('ref_message_id')
    ch_id  = settings.get('ref_channel_id')
    if not msg_id or not ch_id: return
    try:
        ch  = await bot.fetch_channel(int(ch_id))
        msg = await ch.fetch_message(int(msg_id))
        await msg.edit(embed=build_ref_embed(gdata), view=RefBoardView())
    except Exception as e: print(f"[RefBoard update error] {e}")

class EndGameView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    @discord.ui.button(label='🔒 End Game', style=discord.ButtonStyle.danger, custom_id='end_game')
    async def btn_end(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        ref_role = gdata.get('settings', {}).get('ref_role', 'Ref')
        is_ref = any(r.name.lower() == ref_role.lower() for r in interaction.user.roles)
        if not is_ref and not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Only refs can end the game.", ephemeral=True); return
        match = next((m for m in gdata['matches'] if m['id'] == self.match_id), None)
        if not match:
            await interaction.response.send_message("❌  Match not found.", ephemeral=True); return
        if match['status'] != 'ongoing':
            await interaction.response.send_message("❌  Match already ended.", ephemeral=True); return
        await interaction.response.send_message("✅  Game ended! Use `/confirm-result` to submit the final score.", ephemeral=True)
        log_ch_id = gdata.get('settings', {}).get('log_channel_id')
        if log_ch_id:
            try:
                log_ch = await bot.fetch_channel(int(log_ch_id))
                embed = discord.Embed(title=f"📋  Match #{self.match_id} — Ended", colour=discord.Colour.orange())
                embed.add_field(name="Players", value=f"<@{match['p1']}> vs <@{match['p2']}>", inline=True)
                embed.add_field(name="Region",  value=match['region'],                          inline=True)
                embed.add_field(name="Ref",     value=f"<@{interaction.user.id}>",             inline=True)
                embed.set_footer(text="Awaiting result confirmation")
                embed.timestamp = datetime.now(timezone.utc)
                await log_ch.send(embed=embed)
            except Exception as e: print(f"[Log error] {e}")
        if match.get('thread_id'):
            try:
                t = await bot.fetch_channel(int(match['thread_id']))
                await t.edit(archived=True, locked=True)
            except Exception: pass
        if match.get('vc_id'):
            try:
                vc = bot.get_channel(int(match['vc_id']))
                if vc: await vc.delete()
            except Exception: pass
        ref_uid = match.get('ref_uid')
        if ref_uid and ref_uid in gdata.get('active_refs', {}):
            del gdata['active_refs'][ref_uid]
        save_guild(gid, gdata)

async def create_match(guild, gdata, pending, ref_uid):
    gid      = str(guild.id)
    settings = gdata.get('settings', {})
    ch_id    = settings.get('queue_channel_id')
    if not ch_id: return None, None
    try:
        channel  = await bot.fetch_channel(int(ch_id))
        match_id = pending['id']
        p1_q     = pending['p1_data']
        p2_q     = pending['p2_data']

        thread = await channel.create_thread(name=f"Match #{match_id} | {p1_q['name']} vs {p2_q['name']}", type=discord.ChannelType.private_thread, invitable=False)
        for uid in [p1_q['uid'], p2_q['uid'], ref_uid]:
            try:
                m = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                await thread.add_user(m)
            except Exception: pass

        vc = None
        try:
            cat_id   = settings.get('vc_category_id')
            category = guild.get_channel(int(cat_id)) if cat_id else None
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False)}
            for uid in [p1_q['uid'], p2_q['uid'], ref_uid]:
                try:
                    m = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                    overwrites[m] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
                except Exception: pass
            vc = await guild.create_voice_channel(name=f"Match #{match_id} — {p1_q['name']} vs {p2_q['name']}", category=category, overwrites=overwrites)
        except Exception as e: print(f"[VC create error] {e}")

        # Move players into VC
        if vc:
            for uid in [p1_q['uid'], p2_q['uid']]:
                try:
                    m = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                    if m.voice: await m.move_to(vc)
                except Exception: pass

        _, r1, e1, _ = get_rank(p1_q['elo'])
        _, r2, e2, _ = get_rank(p2_q['elo'])
        ref_m = guild.get_member(int(ref_uid)) or await guild.fetch_member(int(ref_uid))

        embed = discord.Embed(title=f"⚔️  Match #{match_id} — First to 5", description=(
            f"**{p1_q['name']}** {e1} {r1} ({p1_q['elo']} ELO)\nvs\n"
            f"**{p2_q['name']}** {e2} {r2} ({p2_q['elo']} ELO)\n\n"
            f"🌍 Region: **{p1_q['region']}**\n"
            f"👮 Ref: **{ref_m.display_name}**\n"
            + (f"🎤 VC: <#{vc.id}>\n" if vc else "") +
            "\n📸 When done, post a screenshot. Ref clicks **End Game** when finished."
        ), colour=discord.Colour.gold())
        embed.set_footer(text=f"Match ID: {match_id}")
        embed.timestamp = datetime.now(timezone.utc)
        await thread.send(embed=embed, view=EndGameView(match_id))

        # DM players with thread + vc link
        for uid in [p1_q['uid'], p2_q['uid']]:
            try:
                m = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                await m.send(
                    f"🎮  **Match found!** (Match #{match_id})\n"
                    f"📝 Go to your match thread: <#{thread.id}>\n"
                    + (f"🎤 Voice channel: <#{vc.id}>\n" if vc else "")
                )
            except Exception: pass

        gdata2 = guild_data(gid)
        gdata2['matches'].append({'id': match_id, 'p1': p1_q['uid'], 'p2': p2_q['uid'], 'p1_name': p1_q['name'], 'p2_name': p2_q['name'], 'p1_elo': p1_q['elo'], 'p2_elo': p2_q['elo'], 'region': p1_q['region'], 'ref_uid': ref_uid, 'status': 'ongoing', 'winner': None, 'p1_score': 0, 'p2_score': 0, 'thread_id': str(thread.id), 'vc_id': str(vc.id) if vc else None, 'created_at': datetime.now(timezone.utc).isoformat()})
        gdata2['pending_matches'] = [m for m in gdata2.get('pending_matches', []) if m['id'] != match_id]
        save_guild(gid, gdata2)
        return thread, vc
    except Exception as e:
        print(f"[create_match error] {e}")
        return None, None

@bot.event
async def on_voice_state_update(member, before, after):
    gid   = str(member.guild.id)
    gdata = guild_data(gid)
    uid   = str(member.id)
    q_vc  = gdata.get('settings', {}).get('queue_vc_id')

    if before.channel and q_vc and str(before.channel.id) == str(q_vc):
        in_match = any(m['status'] == 'ongoing' and uid in (m['p1'], m['p2']) for m in gdata.get('matches', []))
        if not in_match:
            gdata['queue'] = [q for q in gdata.get('queue', []) if q['uid'] != uid]
            cancelled = [m for m in gdata.get('pending_matches', []) if uid in (m['p1'], m['p2']) and m['status'] == 'waiting_for_ref']
            gdata['pending_matches'] = [m for m in gdata.get('pending_matches', []) if m not in cancelled]
            save_guild(gid, gdata)
            try: await member.send("❌  You left the queue VC and were removed from the queue.")
            except Exception: pass
            for m in cancelled:
                other_uid = m['p2'] if uid == m['p1'] else m['p1']
                try:
                    other = member.guild.get_member(int(other_uid)) or await member.guild.fetch_member(int(other_uid))
                    await other.send(f"❌  Your opponent left so **Match #{m['id']}** has been cancelled. You've been put back in the queue!")
                    other_player = get_player(gdata, other_uid)
                    if other_player:
                        gdata2 = guild_data(gid)
                        gdata2.setdefault('queue', []).append({'uid': other_uid, 'name': other_player['name'], 'elo': other_player['elo'], 'region': other_player.get('region', 'EU'), 'queued_at': datetime.now(timezone.utc).isoformat()})
                        save_guild(gid, gdata2)
                except Exception: pass
            await update_ref_board(member.guild, guild_data(gid), guild_data(gid).get('settings', {}))

    if after.channel and q_vc and str(after.channel.id) == str(q_vc):
        player = get_player(gdata, uid)
        if not player:
            try: await member.send("❌  You need to `/register` first!")
            except Exception: pass
            return
        if any(q['uid'] == uid for q in gdata.get('queue', [])):
            return
        if any(m['status'] == 'ongoing' and uid in (m['p1'], m['p2']) for m in gdata.get('matches', [])):
            try: await member.send("❌  You're already in an ongoing match!")
            except Exception: pass
            return
        # Detect region from Discord roles
        region = None
        member_roles = {r.name.upper() for r in member.roles}
        for r in ['EU', 'NA', 'SA', 'AS', 'OCE']:
            if r in member_roles:
                region = r
                break
        if not region:
            region = gdata.get('settings', {}).get('default_region', 'EU')
        gdata.setdefault('queue', []).append({'uid': uid, 'name': player['name'], 'elo': player['elo'], 'region': region, 'kda': round(player['kills'] / max(1, player['deaths']), 2), 'queued_at': datetime.now(timezone.utc).isoformat()})
        save_guild(gid, gdata)
        _, rn, re_, _ = get_rank(player['elo'])
        await try_make_match(gid, gdata)

async def try_make_match(gid, gdata):
    queue = gdata.get('queue', [])
    if len(queue) < 2: return
    best_score = float('inf')
    matched = None
    for i in range(len(queue)):
        for j in range(i + 1, len(queue)):
            p1 = {**queue[i], **get_player(gdata, queue[i]['uid'])}
            p2 = {**queue[j], **get_player(gdata, queue[j]['uid'])}
            # After 3 min expansion, allow cross-region
            qi_wait = (datetime.now(timezone.utc) - datetime.fromisoformat(queue[i]['queued_at'])).total_seconds()
            qj_wait = (datetime.now(timezone.utc) - datetime.fromisoformat(queue[j]['queued_at'])).total_seconds()
            if queue[i]['region'] != queue[j]['region'] and qi_wait < 180 and qj_wait < 180:
                continue
            score = match_score(p1, p2)
            if score < best_score:
                best_score = score
                matched = (i, j)
    if not matched: return
    i, j = matched
    p1_q, p2_q = queue[i], queue[j]
    for idx in sorted([i, j], reverse=True): queue.pop(idx)
    gdata['queue'] = queue
    gdata['match_counter'] = gdata.get('match_counter', 0) + 1
    mid = gdata['match_counter']
    pending = {'id': mid, 'p1': p1_q['uid'], 'p2': p2_q['uid'], 'p1_name': p1_q['name'], 'p2_name': p2_q['name'], 'p1_elo': p1_q['elo'], 'p2_elo': p2_q['elo'], 'region': p1_q['region'], 'status': 'waiting_for_ref', 'p1_data': p1_q, 'p2_data': p2_q, 'created_at': datetime.now(timezone.utc).isoformat()}
    gdata.setdefault('pending_matches', []).append(pending)
    save_guild(gid, gdata)
    guild = next((g for g in bot.guilds if str(g.id) == gid), None)
    if guild:
        await update_ref_board(guild, gdata, gdata.get('settings', {}))

@tasks.loop(minutes=1)
async def queue_expand_task():
    now = datetime.now(timezone.utc)
    all_data = load()
    for gid, gdata in all_data.items():
        queue = gdata.get('queue', [])
        changed = False
        for entry in queue:
            queued_at = datetime.fromisoformat(entry['queued_at'])
            if (now - queued_at).total_seconds() >= 180 and not entry.get('expanded'):
                entry['expanded'] = True
                changed = True
        if changed:
            gdata['queue'] = queue
            all_data[gid] = gdata
    save(all_data)
    for gid, gdata in all_data.items():
        await try_make_match(gid, gdata)

@queue_expand_task.before_loop
async def before_expand(): await bot.wait_until_ready()

@tasks.loop(minutes=5)
async def leaderboard_update_task():
    all_data = load()
    for gid, gdata in all_data.items():
        settings = gdata.get('settings', {})
        lb_msg_id = settings.get('lb_message_id')
        lb_ch_id  = settings.get('lb_channel_id')
        if not lb_msg_id or not lb_ch_id: continue
        try:
            ch  = await bot.fetch_channel(int(lb_ch_id))
            msg = await ch.fetch_message(int(lb_msg_id))
            await msg.edit(embed=build_leaderboard_embed(gdata))
        except Exception as e: print(f"[LB update error] {e}")

@leaderboard_update_task.before_loop
async def before_lb(): await bot.wait_until_ready()

@tasks.loop(seconds=5)
async def ref_board_update_task():
    all_data = load()
    for gid, gdata in all_data.items():
        settings = gdata.get('settings', {})
        msg_id   = settings.get('ref_message_id')
        ch_id    = settings.get('ref_channel_id')
        if not msg_id or not ch_id: continue
        try:
            ch  = await bot.fetch_channel(int(ch_id))
            msg = await ch.fetch_message(int(msg_id))
            await msg.edit(embed=build_ref_embed(gdata), view=RefBoardView())
        except Exception: pass

@ref_board_update_task.before_loop
async def before_ref_board(): await bot.wait_until_ready()

@tasks.loop(minutes=5)
async def discord_persist_task():
    """Push data to Discord every 5 minutes as an extra safety net."""
    await push_to_discord()

@discord_persist_task.before_loop
async def before_discord_persist(): await bot.wait_until_ready()

def build_leaderboard_embed(gdata):
    players = [(uid, p) for uid, p in gdata.get('players', {}).items() if p['wins'] + p['losses'] > 0]
    players.sort(key=lambda x: x[1]['elo'], reverse=True)
    top    = players[:10]
    embed  = discord.Embed(title="🏆  Ranked Leaderboard — Top 10", colour=discord.Colour.gold())
    medals = ['🥇', '🥈', '🥉']
    if not top:
        embed.description = "*No ranked players yet — play some matches!*"
    else:
        lines = []
        for i, (uid, p) in enumerate(top):
            _, rn, re_, _ = get_rank(p['elo'])
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            total = p['wins'] + p['losses']
            wr    = round(p['wins'] / total * 100) if total else 0
            lines.append(f"{medal}  **{p['name']}**\n┣ {re_} {rn}  •  **{p['elo']} ELO**\n┗ {p['wins']}W  {p['losses']}L  •  {wr}% WR\n")
        embed.description = "\n".join(lines)
    embed.set_footer(text="Updates every 5 minutes  •  Last updated")
    embed.timestamp = datetime.now(timezone.utc)
    return embed

@bot.event
async def on_ready():
    print(f"✅  Logged in as {bot.user}")
    # Restore all data from Discord before doing anything else
    restored = await pull_from_discord()
    if not restored: print("[Discord persist] No cloud data found — using local file (or fresh start).")
    bot.add_view(RefBoardView())
    try:
        synced = await bot.tree.sync()
        print(f"✅  Synced {len(synced)} commands: {[c.name for c in synced]}")
    except Exception as e: print(f"❌  Sync failed: {e}")
    queue_expand_task.start()
    leaderboard_update_task.start()
    ref_board_update_task.start()
    discord_persist_task.start()
    print("✅  Bot ready")

@bot.tree.command(name="register", description="Register to play ranked 1v1s")
async def cmd_register(interaction: discord.Interaction):
    try:
        gid, uid = str(interaction.guild_id), str(interaction.user.id)
        gdata = guild_data(gid)
        if uid in gdata['players']:
            await interaction.response.send_message("❌  You're already registered!", ephemeral=True); return
        gdata['players'][uid] = default_player(uid, interaction.user.display_name)
        save_guild(gid, gdata)
        _, rn, re_, colour = get_rank(500)
        embed = discord.Embed(title="✅  Registered!", description=f"Welcome, **{interaction.user.display_name}**!\n\nStarting ELO: **500** | Rank: {re_} **{rn}**\n\nJoin the queue VC to find a match!", colour=colour)
        await interaction.response.send_message(embed=embed)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

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
        total  = player['wins'] + player['losses']
        wr     = round(player['wins'] / total * 100) if total else 0
        kda    = round(player['kills'] / max(1, player['deaths']), 2)
        streak = player.get('streak', 0)
        region = player.get('region', '—')

        embed = discord.Embed(title=f"⚔️  {player['name']}'s Player Card", colour=colour)

        # Banner image (full-width, shown at bottom)
        banners    = gdata.get('settings', {}).get('banners', [])
        banner_idx = player.get('banner', -1)
        if 0 <= banner_idx < len(banners) and banners[banner_idx]:
            embed.set_image(url=banners[banner_idx])

        embed.set_thumbnail(url=target.display_avatar.url)

        # ── Row 1: Rank | [spacer] | Record ──────────────────────────────
        embed.add_field(
            name="🏅  Rank",
            value=f"{rank_emoji} **{rank_name}**\n`{player['elo']} ELO`\n\u200b",
            inline=True
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(
            name="📊  Record",
            value=f"**{player['wins']}W  /  {player['losses']}L**\n`{total} matches  ·  {wr}% WR`\n\u200b",
            inline=True
        )

        # ── Divider ───────────────────────────────────────────────────────
        embed.add_field(name="─" * 37, value="\u200b", inline=False)

        # ── Row 2: Kills | Deaths | KDA ───────────────────────────────────
        embed.add_field(name="🔫  Kills",  value=f"**{player['kills']}**\n\u200b",  inline=True)
        embed.add_field(name="💀  Deaths", value=f"**{player['deaths']}**\n\u200b", inline=True)
        embed.add_field(name="⚡  KDA",    value=f"**{kda}**\n\u200b",              inline=True)

        # ── Footer ────────────────────────────────────────────────────────
        try:
            reg_date = datetime.fromisoformat(player['registered_at']).strftime("%d %b %Y")
        except Exception:
            reg_date = player['registered_at'][:10]
        embed.set_footer(text=f"Registered  ·  {reg_date}")
        embed.timestamp = datetime.now(timezone.utc)
        await interaction.response.send_message(embed=embed)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="set-banner", description="Choose your profile banner")
@app_commands.describe(banner="Which banner to use")
@app_commands.choices(banner=[app_commands.Choice(name=f"{i+1} - {n}", value=i) for i, n in enumerate(BANNER_NAMES)])
async def cmd_set_banner(interaction: discord.Interaction, banner: int):
    try:
        gid, uid = str(interaction.guild_id), str(interaction.user.id)
        gdata  = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message("❌  You need to `/register` first!", ephemeral=True); return
        banners = gdata.get('settings', {}).get('banners', [])
        if banner >= len(banners) or not banners[banner]:
            await interaction.response.send_message(f"❌  Banner **{BANNER_NAMES[banner]}** hasn't been set up yet.", ephemeral=True); return
        gdata['players'][uid]['banner'] = banner
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Banner set to **{BANNER_NAMES[banner]}**!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

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
            result = "✅ **W**" if won else "❌ **L**"
            date   = m.get('completed_at', '')[:10]
            lines.append(f"{result}  •  **#{m['id']}** vs **{opp}**  •  `{my_s}–{op_s}`  •  {date}")
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="queue-status", description="See who's in the queue and pending matches")
async def cmd_queue_status(interaction: discord.Interaction):
    try:
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        queue   = gdata.get('queue', [])
        pending = gdata.get('pending_matches', [])
        embed = discord.Embed(title="🎮  Queue Status", colour=discord.Colour.green())
        if queue:
            lines = []
            for i, q in enumerate(queue):
                _, rn, re_, _ = get_rank(q['elo'])
                lines.append(f"`#{i+1}`  **{q['name']}**  •  {re_} {rn} ({q['elo']} ELO)  •  🌍 {q['region']}")
            embed.add_field(name=f"⏳  In Queue — {len(queue)}", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="⏳  In Queue", value="*Empty*", inline=False)
        if pending:
            lines = [f"**#{m['id']}**  {m['p1_name']} vs {m['p2_name']}  •  🌍 {m['region']}  •  Waiting for ref" for m in pending]
            embed.add_field(name=f"🔍  Waiting for Ref — {len(pending)}", value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="active-matches", description="See all ongoing matches")
async def cmd_active(interaction: discord.Interaction):
    try:
        gid     = str(interaction.guild_id)
        gdata   = guild_data(gid)
        ongoing = [m for m in gdata['matches'] if m['status'] == 'ongoing']
        if not ongoing:
            await interaction.response.send_message("No active matches right now.", ephemeral=True); return
        embed = discord.Embed(title=f"⚔️  Active Matches — {len(ongoing)}", colour=discord.Colour.orange())
        embed.description = "\n".join([f"**#{m['id']}**  •  {m['p1_name']} vs {m['p2_name']}  •  🌍 {m['region']}" for m in ongoing])
        await interaction.response.send_message(embed=embed)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="confirm-result", description="Confirm a match result (Ref only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(match_id="Match ID", winner_id="Discord user ID of the winner", p1_score="Player 1 score", p2_score="Player 2 score")
async def cmd_confirm(interaction: discord.Interaction, match_id: int, winner_id: str, p1_score: int, p2_score: int):
    try:
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        ref_role = gdata.get('settings', {}).get('ref_role', 'Ref')
        is_ref = any(r.name.lower() == ref_role.lower() for r in interaction.user.roles)
        if not is_ref and not interaction.permissions.administrator:
            await interaction.response.send_message(f"❌  You need the **{ref_role}** role.", ephemeral=True); return
        match = next((m for m in gdata['matches'] if m['id'] == match_id), None)
        if not match:
            await interaction.response.send_message(f"❌  Match #{match_id} not found.", ephemeral=True); return
        if winner_id not in (match['p1'], match['p2']):
            await interaction.response.send_message("❌  Winner ID must be one of the two players.", ephemeral=True); return
        loser_id = match['p2'] if winner_id == match['p1'] else match['p1']
        w_p = gdata['players'].get(winner_id)
        l_p = gdata['players'].get(loser_id)
        if not w_p or not l_p:
            await interaction.response.send_message("❌  Player data not found.", ephemeral=True); return
        old_w, old_l = w_p['elo'], l_p['elo']
        new_w, new_l, gained, lost = new_elos(old_w, old_l)
        w_p['elo'] = new_w; w_p['wins'] += 1
        w_p['kills']  += p1_score if winner_id == match['p1'] else p2_score
        w_p['deaths'] += p2_score if winner_id == match['p1'] else p1_score
        l_p['elo'] = max(100, new_l); l_p['losses'] += 1
        l_p['kills']  += p2_score if winner_id == match['p1'] else p1_score
        l_p['deaths'] += p1_score if winner_id == match['p1'] else p2_score
        match.update({'status': 'completed', 'winner': winner_id, 'p1_score': p1_score, 'p2_score': p2_score, 'confirmed_by': str(interaction.user.id), 'completed_at': datetime.now(timezone.utc).isoformat(), 'elo_gained': gained, 'elo_lost': lost})
        w_p['matches'].append(match_id)
        l_p['matches'].append(match_id)
        ref_uid = match.get('ref_uid')
        if ref_uid and ref_uid in gdata.get('active_refs', {}):
            del gdata['active_refs'][ref_uid]
        save_guild(gid, gdata)
        _, wr, we, wc = get_rank(new_w)
        _, lr, le, _  = get_rank(new_l)
        embed = discord.Embed(title=f"✅  Match #{match_id} — Result Confirmed", colour=wc)
        embed.add_field(name="🏆  Winner", value=f"<@{winner_id}>\n{we} {wr}\n{old_w} → **{new_w}** ELO (+{gained})", inline=True)
        embed.add_field(name="💀  Loser",  value=f"<@{loser_id}>\n{le} {lr}\n{old_l} → **{new_l}** ELO (-{lost})", inline=True)
        embed.add_field(name="📊  Score",  value=f"**{p1_score} — {p2_score}**", inline=False)
        embed.set_footer(text=f"Confirmed by {interaction.user.display_name}")
        embed.timestamp = datetime.now(timezone.utc)
        await interaction.response.send_message(embed=embed)
        log_ch_id = gdata.get('settings', {}).get('log_channel_id')
        if log_ch_id:
            try:
                log_ch = await bot.fetch_channel(int(log_ch_id))
                await log_ch.send(embed=embed)
            except Exception: pass
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="rollback", description="Roll back last N matches for a player (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Player to roll back", games="Number of recent games to reverse")
async def cmd_rollback(interaction: discord.Interaction, user: discord.Member, games: int):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid, uid = str(interaction.guild_id), str(user.id)
        gdata  = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message("❌  Player not registered.", ephemeral=True); return
        completed = [m for m in gdata['matches'] if m['status'] == 'completed' and uid in (m['p1'], m['p2'])]
        completed.sort(key=lambda x: x.get('completed_at', ''), reverse=True)
        to_rb = completed[:games]
        if not to_rb:
            await interaction.response.send_message("❌  No completed matches to roll back.", ephemeral=True); return
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
        embed = discord.Embed(title="↩️  Rollback Complete", colour=discord.Colour.orange())
        embed.add_field(name="Player",            value=f"<@{uid}>",                                                    inline=True)
        embed.add_field(name="Games Rolled Back", value=f"**{len(rolled)}**  (#{', #'.join(str(x) for x in rolled)})", inline=True)
        embed.add_field(name="ELO Change",        value=f"{old_elo} → **{player['elo']}**",                            inline=False)
        embed.set_footer(text=f"By {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="adjust-elo", description="Add or remove ELO from a player (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="The player", amount="Amount e.g. 50 or -50")
async def cmd_adjust_elo(interaction: discord.Interaction, user: discord.Member, amount: int):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid, uid = str(interaction.guild_id), str(user.id)
        gdata  = guild_data(gid)
        player = get_player(gdata, uid)
        if not player:
            await interaction.response.send_message("❌  Player not registered.", ephemeral=True); return
        old_elo = player['elo']
        player['elo'] = max(100, player['elo'] + amount)
        save_guild(gid, gdata)
        _, rn, re_, colour = get_rank(player['elo'])
        sign = "+" if amount >= 0 else ""
        embed = discord.Embed(title="⚙️  ELO Adjusted", description=f"<@{uid}>\n{old_elo} → **{player['elo']}** ELO  ({sign}{amount})\n{re_} {rn}", colour=colour)
        embed.set_footer(text=f"By {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-data-channel", description="Set the private channel where bot data is saved permanently (Admin only — run IN the channel)")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_data_channel(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['data_channel_id'] = str(interaction.channel_id)
        save_guild(gid, gdata)
        await interaction.response.send_message(
            f"✅  Data will be saved to <#{interaction.channel_id}> and restored automatically on every restart.\n"
            f"⚠️  Keep this channel **private** (only the bot should see it).",
            ephemeral=True
        )
        # Push immediately so the first save is there right away
        await push_to_discord()
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-queue", description="Set channel where match threads are created — run IN the channel (Admin only)")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_queue(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['queue_channel_id'] = str(interaction.channel_id)
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Match threads will be created in <#{interaction.channel_id}>!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-queue-vc", description="Set the single queue VC — join it first, then run this (Admin only)")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_queue_vc(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌  Join the queue VC first, then run this!", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        vc = interaction.user.voice.channel
        gdata.setdefault('settings', {})['queue_vc_id'] = str(vc.id)
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  **{vc.name}** is now the queue VC!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-default-region", description="Set the default region for the queue VC (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(region="Default region for players joining the queue VC")
@app_commands.choices(region=[app_commands.Choice(name=r, value=r) for r in REGIONS])
async def cmd_setup_default_region(interaction: discord.Interaction, region: str):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['default_region'] = region
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Default region set to **{region}**!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-ref-role", description="Set the referee role name (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(role_name="Exact name of the ref role")
async def cmd_setup_ref_role(interaction: discord.Interaction, role_name: str):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['ref_role'] = role_name
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Ref role set to **{role_name}**!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-vc-category", description="Set the category where match VCs are created — run in any channel in that category (Admin only)")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_vc_category(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        if not interaction.channel.category:
            await interaction.response.send_message("❌  This channel isn't in a category!", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['vc_category_id'] = str(interaction.channel.category.id)
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Match VCs will be created in the **{interaction.channel.category.name}** category!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-log-channel", description="Set the channel where match results are logged — run IN the channel (Admin only)")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_log(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['log_channel_id'] = str(interaction.channel_id)
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Match results will be logged in <#{interaction.channel_id}>!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="post-ref-board", description="Post the ref availability board (Admin only)")
@app_commands.default_permissions(administrator=True)
async def cmd_post_ref_board(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        msg   = await interaction.channel.send(embed=build_ref_embed(gdata), view=RefBoardView())
        gdata.setdefault('settings', {})['ref_message_id'] = str(msg.id)
        gdata['settings']['ref_channel_id'] = str(interaction.channel_id)
        save_guild(gid, gdata)
        await interaction.response.send_message("✅  Ref board posted!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="post-leaderboard", description="Post a live leaderboard that updates every 5 minutes (Admin only)")
@app_commands.default_permissions(administrator=True)
async def cmd_post_leaderboard(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        msg   = await interaction.channel.send(embed=build_leaderboard_embed(gdata))
        gdata.setdefault('settings', {})['lb_message_id'] = str(msg.id)
        gdata['settings']['lb_channel_id'] = str(interaction.channel_id)
        save_guild(gid, gdata)
        await interaction.response.send_message("✅  Live leaderboard posted!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-banner-storage", description="Set the private channel where banner images are stored permanently (Admin only)")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_banner_storage(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid = str(interaction.guild_id)
        gdata = guild_data(gid)
        gdata.setdefault('settings', {})['banner_storage_channel_id'] = str(interaction.channel_id)
        save_guild(gid, gdata)
        await interaction.response.send_message(
            f"✅  Banner images will be stored in <#{interaction.channel_id}>.\n"
            f"💡  Keep this channel **private** — it just holds the image files so they survive redeployments.",
            ephemeral=True
        )
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-banner", description="Set a profile banner by uploading an image (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(image="Upload the banner image directly")
@app_commands.choices(slot=[app_commands.Choice(name=f"{i+1} - {n}", value=i+1) for i, n in enumerate(BANNER_NAMES)])
async def cmd_setup_banner(interaction: discord.Interaction, slot: int, image: discord.Attachment):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        if not image.content_type or not image.content_type.startswith('image/'):
            await interaction.response.send_message("❌  Please upload an image file.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        gid   = str(interaction.guild_id)
        gdata = guild_data(gid)
        banners = gdata.setdefault('settings', {}).setdefault('banners', [''] * 6)
        while len(banners) < 6: banners.append('')

        # Re-upload to the permanent storage channel so the URL survives redeployments.
        storage_ch_id = gdata.get('settings', {}).get('banner_storage_channel_id')
        if storage_ch_id:
            try:
                storage_ch = await bot.fetch_channel(int(storage_ch_id))
                async with aiohttp.ClientSession() as session:
                    async with session.get(image.url) as resp:
                        img_bytes = await resp.read()
                ext = image.filename.rsplit('.', 1)[-1] if '.' in image.filename else 'png'
                file = discord.File(io.BytesIO(img_bytes), filename=f"banner_{slot}.{ext}")
                stored_msg = await storage_ch.send(
                    content=f"🖼️  Banner slot {slot} — **{BANNER_NAMES[slot-1]}**",
                    file=file
                )
                permanent_url = stored_msg.attachments[0].url
                banners[slot - 1] = permanent_url
                gdata['settings']['banners'] = banners
                save_guild(gid, gdata)
                await interaction.followup.send(
                    f"✅  Banner **{BANNER_NAMES[slot-1]}** saved permanently to <#{storage_ch_id}>!", ephemeral=True
                )
                return
            except Exception as e:
                print(f"[Banner storage error] {e}")
                # Fall through to saving the raw URL with a warning

        # No storage channel set — save the raw CDN URL (may expire on redeploy)
        banners[slot - 1] = image.url
        gdata['settings']['banners'] = banners
        save_guild(gid, gdata)
        await interaction.followup.send(
            f"✅  Banner **{BANNER_NAMES[slot-1]}** set!\n"
            f"⚠️  Run `/setup-banner-storage` in a private channel first so banners survive redeployments.",
            ephemeral=True
        )
    except Exception as e: await interaction.followup.send(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="reset-elo", description="Reset a player's ELO to 500 (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="The player to reset")
async def cmd_reset_elo(interaction: discord.Interaction, user: discord.Member):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid, uid = str(interaction.guild_id), str(user.id)
        gdata = guild_data(gid)
        if uid not in gdata['players']:
            await interaction.response.send_message("❌  Player not registered.", ephemeral=True); return
        gdata['players'][uid]['elo'] = 500
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  Reset **{user.display_name}**'s ELO to 500.", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="unregister", description="Remove a player from the system (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="The player to remove")
async def cmd_unregister(interaction: discord.Interaction, user: discord.Member):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        gid, uid = str(interaction.guild_id), str(user.id)
        gdata = guild_data(gid)
        if uid not in gdata['players']:
            await interaction.response.send_message("❌  Player not registered.", ephemeral=True); return
        del gdata['players'][uid]
        gdata['queue'] = [q for q in gdata['queue'] if q['uid'] != uid]
        save_guild(gid, gdata)
        await interaction.response.send_message(f"✅  **{user.display_name}** has been removed.", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

if __name__ == '__main__':
    if not TOKEN: raise ValueError("DISCORD_TOKEN not set!")
    bot.run(TOKEN)
