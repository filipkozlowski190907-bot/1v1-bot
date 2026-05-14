import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import re
from datetime import datetime, timezone

TOKEN     = os.getenv('DISCORD_TOKEN')
DATA_FILE = 'guilds.json'
AUTO_POST_WEEKDAY = 2
AUTO_POST_HOUR    = 12

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

def load_all():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_all(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def default_sheet():
    return {'channel_id': None, 'broadcaster': None, 'commentators': [], 'staff': [], 'message_id': None, 'lock_at': None, 'locked': False, 'title': '', 'description': ''}

def get_sheet(guild_id, sheet):
    return load_all().get(guild_id, {}).get(sheet, default_sheet())

def save_sheet(guild_id, sheet, data):
    all_data = load_all()
    if guild_id not in all_data: all_data[guild_id] = {}
    all_data[guild_id][sheet] = data
    save_all(all_data)

def save_guild_meta(guild_id, meta):
    all_data = load_all()
    if guild_id not in all_data: all_data[guild_id] = {}
    all_data[guild_id].update(meta)
    save_all(all_data)

def build_embed(data, sheet_label):
    locked  = data.get('locked', False)
    lock_at = data.get('lock_at')
    title   = data.get('title') or sheet_label
    desc    = data.get('description', '')
    close_text = f"\n\U0001f550 {'Closed at' if locked else 'Closes'}: {lock_at}" if lock_at else ""
    body = f"{desc}{close_text}".strip() or ("\u007e\u007eSign-ups are now closed.\u007e\u007e" if locked else "Click a button below to claim your role!")
    embed = discord.Embed(title=f"{'🔒' if locked else '📅'}  {title}", description=body, colour=discord.Colour.red() if locked else discord.Colour.blurple())
    embed.add_field(name=f"📡  Broadcaster  [{'1/1' if data['broadcaster'] else '0/1'}]", value=f"✅ <@{data['broadcaster']}>" if data['broadcaster'] else "*Open — be the first!*", inline=False)
    embed.add_field(name=f"🎙️  Commentators  [{len(data['commentators'])}]", value="\n".join(f"• <@{u}>" for u in data['commentators']) or "*No signups yet*", inline=False)
    embed.add_field(name=f"🛠️  Staff  [{len(data['staff'])}]", value="\n".join(f"• <@{u}>" for u in data['staff']) or "*No signups yet*", inline=False)
    embed.set_footer(text="Sign-ups reset each week  •  You can hold multiple roles")
    embed.timestamp = datetime.now(timezone.utc)
    return embed

def make_view(sheet_key, event_label):
    class SignupView(discord.ui.View):
        SHEET = sheet_key
        def __init__(self): super().__init__(timeout=None)
        def _role(self, m, *names): return any(n.lower() in {r.name.lower() for r in m.roles} for n in names)
        async def _refresh(self, interaction, data):
            try:
                ch = interaction.channel if str(interaction.channel_id) == str(data.get('channel_id')) else await interaction.client.fetch_channel(int(data['channel_id']))
                msg = await ch.fetch_message(int(data['message_id']))
                print(f"[Refresh] sheet={self.SHEET}, message_id={data['message_id']}, channel_id={data['channel_id']}")
                await msg.edit(embed=build_embed(data, "The Games" if self.SHEET == 'tg' else "ZeroC"))
            except Exception as e: print(f"[Refresh error] sheet={self.SHEET}, error={e}, message_id={data.get('message_id')}")

        @discord.ui.button(label=f'📡 Broadcaster — {event_label}', style=discord.ButtonStyle.danger, custom_id=f'sb_{sheet_key}')
        async def btn_bc(self, interaction, button):
            if not self._role(interaction.user, 'The Broadcasters'):
                await interaction.response.send_message("❌  You need the **The Broadcasters** role.", ephemeral=True); return
            data = get_sheet(str(interaction.guild_id), self.SHEET)
            if data.get('locked'): await interaction.response.send_message("🔒  Sign-ups are closed.", ephemeral=True); return
            uid = str(interaction.user.id)
            if data['broadcaster'] == uid: await interaction.response.send_message("Already signed up as Broadcaster!", ephemeral=True); return
            if data['broadcaster']: await interaction.response.send_message(f"Slot taken by <@{data['broadcaster']}>!", ephemeral=True); return
            data['broadcaster'] = uid; save_sheet(str(interaction.guild_id), self.SHEET, data)
            await self._refresh(interaction, data); await interaction.response.send_message("✅  Signed up as **Broadcaster**!", ephemeral=True)

        @discord.ui.button(label=f'🎙️ Commentator — {event_label}', style=discord.ButtonStyle.primary, custom_id=f'sc_{sheet_key}')
        async def btn_co(self, interaction, button):
            if not self._role(interaction.user, 'The Commentators'):
                await interaction.response.send_message("❌  You need the **The Commentators** role.", ephemeral=True); return
            data = get_sheet(str(interaction.guild_id), self.SHEET)
            if data.get('locked'): await interaction.response.send_message("🔒  Sign-ups are closed.", ephemeral=True); return
            uid = str(interaction.user.id)
            if uid in data['commentators']: await interaction.response.send_message("Already signed up as Commentator!", ephemeral=True); return
            data['commentators'].append(uid); save_sheet(str(interaction.guild_id), self.SHEET, data)
            await self._refresh(interaction, data); await interaction.response.send_message("✅  Signed up as **Commentator**!", ephemeral=True)

        @discord.ui.button(label=f'🛠️ Staff — {event_label}', style=discord.ButtonStyle.success, custom_id=f'ss_{sheet_key}')
        async def btn_st(self, interaction, button):
            if not self._role(interaction.user, 'The Peacekeepers', 'The Authority'):
                await interaction.response.send_message("❌  You need **The Peacekeepers** or **The Authority** role.", ephemeral=True); return
            data = get_sheet(str(interaction.guild_id), self.SHEET)
            is_pk = self._role(interaction.user, 'The Peacekeepers')
            if data.get('locked') and not is_pk: await interaction.response.send_message("🔒  Sign-ups are closed.", ephemeral=True); return
            uid = str(interaction.user.id)
            if uid in data['staff']: await interaction.response.send_message("Already signed up as Staff!", ephemeral=True); return
            data['staff'].append(uid); save_sheet(str(interaction.guild_id), self.SHEET, data)
            await self._refresh(interaction, data); await interaction.response.send_message("✅  Signed up as **Staff**!", ephemeral=True)

        @discord.ui.button(label='🔒 Lock', style=discord.ButtonStyle.danger, custom_id=f'lk_{sheet_key}')
        async def btn_lock(self, interaction, button):
            if not interaction.permissions.administrator: await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
            data = get_sheet(str(interaction.guild_id), self.SHEET)
            if data.get('locked'): await interaction.response.send_message("Already locked.", ephemeral=True); return
            data['locked'] = True; save_sheet(str(interaction.guild_id), self.SHEET, data)
            await self._refresh(interaction, data); await interaction.response.send_message("🔒  Locked.", ephemeral=True)

        @discord.ui.button(label='🔓 Unlock', style=discord.ButtonStyle.success, custom_id=f'ul_{sheet_key}')
        async def btn_unlock(self, interaction, button):
            if not interaction.permissions.administrator: await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
            data = get_sheet(str(interaction.guild_id), self.SHEET)
            if not data.get('locked'): await interaction.response.send_message("Already unlocked.", ephemeral=True); return
            data['locked'] = False; save_sheet(str(interaction.guild_id), self.SHEET, data)
            await self._refresh(interaction, data); await interaction.response.send_message("🔓  Unlocked.", ephemeral=True)

        @discord.ui.button(label='❌ Remove my signup', style=discord.ButtonStyle.secondary, custom_id=f'rm_{sheet_key}')
        async def btn_remove(self, interaction, button):
            data = get_sheet(str(interaction.guild_id), self.SHEET)
            uid = str(interaction.user.id)
            is_pk = self._role(interaction.user, 'The Peacekeepers')
            if data.get('locked') and not is_pk: await interaction.response.send_message("🔒  Sign-ups are closed.", ephemeral=True); return
            was = data['broadcaster'] == uid or uid in data['commentators'] or uid in data['staff']
            if not was: await interaction.response.send_message("No active signup found.", ephemeral=True); return
            if data['broadcaster'] == uid: data['broadcaster'] = None
            if uid in data['commentators']: data['commentators'].remove(uid)
            if uid in data['staff']: data['staff'].remove(uid)
            save_sheet(str(interaction.guild_id), self.SHEET, data)
            await self._refresh(interaction, data); await interaction.response.send_message("✅  Signup removed.", ephemeral=True)

    SignupView.__name__ = f'SignupView_{sheet_key}'
    return SignupView

SignupViewTG = make_view('tg', 'The Games')
SignupViewZC = make_view('zc', 'ZeroC')

async def post_signup(channel, guild_id, sheet, title, description, lock_at=None):
    label = "The Games" if sheet == 'tg' else "ZeroC"
    data = {'channel_id': str(channel.id), 'broadcaster': None, 'commentators': [], 'staff': [], 'message_id': None, 'lock_at': lock_at, 'locked': False, 'title': title or label, 'description': description or ''}
    save_sheet(guild_id, sheet, data)
    view = SignupViewTG() if sheet == 'tg' else SignupViewZC()
    msg = await channel.send(embed=build_embed(data, label), view=view)
    data['message_id'] = str(msg.id); save_sheet(guild_id, sheet, data)

@tasks.loop(minutes=1)
async def auto_lock_task():
    now = datetime.now(timezone.utc)
    all_data = load_all()
    for guild_id, gdata in all_data.items():
        for sheet in ('tg', 'zc'):
            sdata = gdata.get(sheet, {})
            if not sdata or sdata.get('locked') or not sdata.get('lock_at'): continue
            try:
                raw = sdata['lock_at'].strip()
                match = re.search(r'<t:(\d+)(?::[a-zA-Z])?>', raw)
                lt = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc) if match else datetime.strptime(raw, '%d/%m/%Y %H:%M').replace(tzinfo=timezone.utc)
                if now >= lt:
                    sdata['locked'] = True; gdata[sheet] = sdata; all_data[guild_id] = gdata; save_all(all_data)
                    if sdata.get('channel_id') and sdata.get('message_id'):
                        ch = await bot.fetch_channel(int(sdata['channel_id']))
                        msg = await ch.fetch_message(int(sdata['message_id']))
                        await msg.edit(embed=build_embed(sdata, "The Games" if sheet == 'tg' else "ZeroC"))
            except Exception as e: print(f"[AutoLock error] {e}")

@auto_lock_task.before_loop
async def before_auto(): await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"✅  Logged in as {bot.user}")
    bot.add_view(SignupViewTG()); bot.add_view(SignupViewZC())
    try:
        synced = await bot.tree.sync()
        print(f"✅  Synced {len(synced)} commands: {[c.name for c in synced]}")
    except Exception as e: print(f"❌  Sync failed: {e}")
    auto_lock_task.start()

@bot.tree.command(name="setup-thegames", description="Set channel for The Games sign-ups — run IN the channel (Admin only)")
async def cmd_setup_tg(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator: await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        data = get_sheet(str(interaction.guild_id), 'tg'); data['channel_id'] = str(interaction.channel_id)
        save_sheet(str(interaction.guild_id), 'tg', data)
        await interaction.response.send_message(f"✅  The Games channel set to <#{interaction.channel_id}>!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="setup-zeroc", description="Set channel for ZeroC sign-ups — run IN the channel (Admin only)")
async def cmd_setup_zc(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator: await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        data = get_sheet(str(interaction.guild_id), 'zc'); data['channel_id'] = str(interaction.channel_id)
        save_sheet(str(interaction.guild_id), 'zc', data)
        await interaction.response.send_message(f"✅  ZeroC channel set to <#{interaction.channel_id}>!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="post-thegames", description="Post The Games sign-up sheet (Admin only)")
@app_commands.describe(title="Title for the sign-up", description="Description shown on the sheet", lock_at="Auto-lock time (Discord @time or DD/MM/YYYY HH:MM)")
async def cmd_post_tg(interaction: discord.Interaction, title: str = 'The Games', description: str = '', lock_at: str = None):
    try:
        if not interaction.permissions.administrator: await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        await post_signup(interaction.channel, str(interaction.guild_id), 'tg', title, description, lock_at)
        await interaction.response.send_message("✅  The Games sign-up posted!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="post-zeroc", description="Post ZeroC sign-up sheet (Admin only)")
@app_commands.describe(title="Title for the sign-up", description="Description shown on the sheet", lock_at="Auto-lock time (Discord @time or DD/MM/YYYY HH:MM)")
async def cmd_post_zc(interaction: discord.Interaction, title: str = 'ZeroC', description: str = '', lock_at: str = None):
    try:
        if not interaction.permissions.administrator: await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        await post_signup(interaction.channel, str(interaction.guild_id), 'zc', title, description, lock_at)
        await interaction.response.send_message("✅  ZeroC sign-up posted!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="remove-signup", description="Remove a user's signup by their Discord ID (Admin only)")
@app_commands.describe(user_id="The Discord user ID to remove", sheet="Which sign-up sheet")
@app_commands.choices(sheet=[app_commands.Choice(name="The Games", value="tg"), app_commands.Choice(name="ZeroC", value="zc")])
async def cmd_remove_signup(interaction: discord.Interaction, user_id: str, sheet: str):
    try:
        if not interaction.permissions.administrator: await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        data = get_sheet(str(interaction.guild_id), sheet)
        was = data['broadcaster'] == user_id or user_id in data['commentators'] or user_id in data['staff']
        if not was: await interaction.response.send_message(f"❌  No signup found for `{user_id}`.", ephemeral=True); return
        if data['broadcaster'] == user_id: data['broadcaster'] = None
        if user_id in data['commentators']: data['commentators'].remove(user_id)
        if user_id in data['staff']: data['staff'].remove(user_id)
        save_sheet(str(interaction.guild_id), sheet, data)
        if data.get('channel_id') and data.get('message_id'):
            try:
                ch = await bot.fetch_channel(int(data['channel_id']))
                msg = await ch.fetch_message(int(data['message_id']))
                await msg.edit(embed=build_embed(data, "The Games" if sheet == 'tg' else "ZeroC"))
            except Exception: pass
        await interaction.response.send_message(f"✅  Removed signup for <@{user_id}>.", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="clear-signups", description="Clear all signups on a sheet (Admin only)")
@app_commands.describe(sheet="Which sheet to clear")
@app_commands.choices(sheet=[app_commands.Choice(name="The Games", value="tg"), app_commands.Choice(name="ZeroC", value="zc")])
async def cmd_clear(interaction: discord.Interaction, sheet: str):
    try:
        if not interaction.permissions.administrator: await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        data = get_sheet(str(interaction.guild_id), sheet)
        data['broadcaster'] = None; data['commentators'] = []; data['staff'] = []
        save_sheet(str(interaction.guild_id), sheet, data)
        if data.get('channel_id') and data.get('message_id'):
            try:
                ch = await bot.fetch_channel(int(data['channel_id']))
                msg = await ch.fetch_message(int(data['message_id']))
                await msg.edit(embed=build_embed(data, "The Games" if sheet == 'tg' else "ZeroC"))
            except Exception: pass
        await interaction.response.send_message("✅  Signups cleared.", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="view-signups", description="See who's signed up on a sheet")
@app_commands.describe(sheet="Which sheet to view")
@app_commands.choices(sheet=[app_commands.Choice(name="The Games", value="tg"), app_commands.Choice(name="ZeroC", value="zc")])
async def cmd_view(interaction: discord.Interaction, sheet: str):
    data = get_sheet(str(interaction.guild_id), sheet)
    await interaction.response.send_message(embed=build_embed(data, "The Games" if sheet == 'tg' else "ZeroC"), ephemeral=True)

class LOAModal(discord.ui.Modal, title='Leave of Absence Request'):
    discord_name  = discord.ui.TextInput(label='Your Discord Name', placeholder='e.g. roxas', required=True)
    events_missed = discord.ui.TextInput(label='How many upcoming events will you miss?', placeholder='e.g. 2', required=True)
    reason        = discord.ui.TextInput(label='Reason', placeholder='Why will you be unavailable?', style=discord.TextStyle.paragraph, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            loa_ch_id = load_all().get(str(interaction.guild_id), {}).get('loa_channel_id')
            if not loa_ch_id: await interaction.response.send_message("❌  No LOA channel set! Admin needs to run `/setup-loa` first.", ephemeral=True); return
            ch = await bot.fetch_channel(int(loa_ch_id))
            embed = discord.Embed(title="📋  Leave of Absence Request", description="Only visible to **The Capitol** and **Director**.", colour=discord.Colour.orange())
            embed.add_field(name="👤  Discord Name",  value=self.discord_name.value,   inline=False)
            embed.add_field(name="🎮  Events Missed", value=self.events_missed.value,  inline=True)
            embed.add_field(name="📝  Reason",        value=self.reason.value,         inline=False)
            embed.set_footer(text=f"Submitted by {interaction.user} (ID: {interaction.user.id})")
            embed.timestamp = datetime.now(timezone.utc)
            await ch.send(embed=embed)
            await interaction.response.send_message("✅  LOA submitted! Only **The Capitol** and **Director** can see it.", ephemeral=True)
        except Exception as e:
            try: await interaction.response.send_message(f"❌  {e}", ephemeral=True)
            except Exception: pass

@bot.tree.command(name="loa", description="Submit a Leave of Absence — only seen by The Capitol and Director")
async def cmd_loa(interaction: discord.Interaction):
    try:
        allowed = {'staff', 'the peacekeepers', 'the authority'}
        if not {r.name.lower() for r in interaction.user.roles}.intersection(allowed):
            await interaction.response.send_message("❌  You need **Staff**, **The Peacekeepers**, or **The Authority** role.", ephemeral=True); return
        await interaction.response.send_modal(LOAModal())
    except Exception as e:
        try: await interaction.response.send_message(f"❌  {e}", ephemeral=True)
        except Exception: pass

@bot.tree.command(name="setup-loa", description="Set the LOA channel — run IN the channel (Admin only)")
async def cmd_setup_loa(interaction: discord.Interaction):
    try:
        if not interaction.permissions.administrator: await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        save_guild_meta(str(interaction.guild_id), {'loa_channel_id': str(interaction.channel_id)})
        await interaction.response.send_message(f"✅  LOA channel set to <#{interaction.channel_id}>!", ephemeral=True)
    except Exception as e: await interaction.response.send_message(f"❌  {e}", ephemeral=True)

@bot.tree.command(name="zeroc-schedule", description="Post the ZeroC schedule (Admin only)")
@app_commands.describe(
    eu_time="Discord @time for the EU ZeroC event",
    na_time="Discord @time for the NA ZeroC event",
    month="Month label (e.g. April, 2026) — defaults to current month",
)
async def cmd_schedule(interaction: discord.Interaction, eu_time: str, na_time: str, month: str = None):
    try:
        if not interaction.permissions.administrator:
            await interaction.response.send_message("❌  Admins only.", ephemeral=True); return
        now = datetime.now(timezone.utc)
        month_label = month or now.strftime("%B, %Y")
        embed = discord.Embed(
            title="🏆  ZeroC — Official Schedule",
            description=f"### 📅  {month_label}",
            colour=discord.Colour.from_rgb(255, 165, 0),
        )
        embed.add_field(name="🌍  EU Event", value=f"> {eu_time}\n> ZeroC EU Event", inline=False)
        embed.add_field(name="🌎  NA Event", value=f"> {na_time}\n> ZeroC NA Event", inline=False)
        embed.add_field(name="⏰  Time Zones", value="All times are automatically converted to your local time zone.", inline=False)
        embed.set_footer(text="ZeroC Competitive")
        embed.timestamp = datetime.now(timezone.utc)
        await interaction.channel.send(embed=embed)
        await interaction.response.send_message("✅  Schedule posted!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌  {e}", ephemeral=True)

if __name__ == '__main__':
    if not TOKEN: raise ValueError("DISCORD_TOKEN not set!")
    bot.run(TOKEN)
