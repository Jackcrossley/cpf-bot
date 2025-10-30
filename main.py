# main.py
import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import sqlite3
import datetime
from flask import Flask
from threading import Thread
from typing import Optional

# ---------- Keep-alive web server (for Replit / uptime pingers) ----------
app = Flask('')

@app.route('/')
def home():
    return "F1 League Bot — keep-alive"

def run_web():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_web, daemon=True).start()

# ---------- Config ----------
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable!")

# default steward role name; editable by /setsystem stewardrole
DEFAULT_STEWARD_ROLE = "Steward"

# ---------- Intents & Bot ----------
intents = discord.Intents.default()
intents.members = True
intents.message_content = False  # we use slash commands
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- Database ----------
DB = "league.db"
conn = sqlite3.connect(DB, check_same_thread=False)
c = conn.cursor()

# tables
c.executescript("""
CREATE TABLE IF NOT EXISTS drivers (
    user_id TEXT PRIMARY KEY,
    name TEXT
);

CREATE TABLE IF NOT EXISTS penalties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    points INTEGER,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    type TEXT, -- 'quali' or 'race'
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attendance (
    message_id TEXT,
    user_id TEXT,
    status TEXT, -- attend / not / maybe
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS settings (
    guild_id TEXT PRIMARY KEY,
    welcome_channel_id TEXT,
    goodbye_channel_id TEXT,
    ticket_log_channel_id TEXT,
    steward_role_name TEXT DEFAULT 'Steward',
    ticket_category_id TEXT
    welcome_message TEXT,
    goodbye_message TEXT


);


CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT,
    channel_id TEXT,
    owner_id TEXT,
    opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    closed_at DATETIME
);

CREATE TABLE IF NOT EXISTS banlist_messages (
    guild_id TEXT PRIMARY KEY,
    channel_id TEXT,
    message_id TEXT
);

""")
conn.commit()

# ---------- Helpers ----------
def ensure_driver_exists(user_id: str, name: str):
    c.execute('INSERT OR IGNORE INTO drivers (user_id, name) VALUES (?, ?)', (user_id, name))
    conn.commit()

def get_steward_role_name(guild_id: int) -> str:
    c.execute('SELECT steward_role_name FROM settings WHERE guild_id = ?', (str(guild_id),))
    r = c.fetchone()
    return r[0] if r and r[0] else DEFAULT_STEWARD_ROLE

def is_steward_member(member: discord.Member) -> bool:
    role_name = get_steward_role_name(member.guild.id)
    return any(role.name.lower() == role_name.lower() for role in member.roles)

def update_auto_bans(member_id: str, member_name: Optional[str] = None) -> int:
    """Calculate total points, insert/remove automatic quals/race bans. Return total points."""
    if member_name:
        ensure_driver_exists(member_id, member_name)
    c.execute('SELECT SUM(points) FROM penalties WHERE user_id = ?', (member_id,))
    total = c.fetchone()[0] or 0

    # quali >=10
    c.execute('SELECT id FROM bans WHERE user_id = ? AND type = "quali"', (member_id,))
    has_quali = c.fetchone() is not None
    if total >= 10 and not has_quali:
        c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "quali", ?, CURRENT_TIMESTAMP)',
                  (member_id, "Automatic quali ban for 10+ points"))
    elif total < 10 and has_quali:
        c.execute('DELETE FROM bans WHERE user_id = ? AND type = "quali"', (member_id,))

    # race >=15
    c.execute('SELECT id FROM bans WHERE user_id = ? AND type = "race"', (member_id,))
    has_race = c.fetchone() is not None
    if total >= 15 and not has_race:
        c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "race", ?, CURRENT_TIMESTAMP)',
                  (member_id, "Automatic race ban for 15+ points"))
    elif total < 15 and has_race:
        c.execute('DELETE FROM bans WHERE user_id = ? AND type = "race"', (member_id,))

    conn.commit()
    return total

def cleanup_expired_bans_db():
    cutoff = datetime.datetime.now() - datetime.timedelta(days=8)
    c.execute('SELECT id, timestamp FROM bans')
    rows = c.fetchall()
    for ban_id, ts in rows:
        try:
            ban_time = datetime.datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        except Exception:
            try:
                ban_time = datetime.datetime.fromisoformat(ts)
            except Exception:
                ban_time = None
        if ban_time and ban_time < cutoff:
            c.execute('DELETE FROM bans WHERE id = ?', (ban_id,))
    conn.commit()

# ---------- Live Ban List Update ----------
async def update_live_banlist(guild: discord.Guild):
    # get the message info from DB
    c.execute('SELECT banlist_message_id, banlist_channel_id FROM settings WHERE guild_id = ?', (str(guild.id),))
    r = c.fetchone()
    if not r or not r[0] or not r[1]:
        return  # nothing to update
    try:
        channel = guild.get_channel(int(r[1]))
        msg = await channel.fetch_message(int(r[0]))
    except Exception:
        return

    # fetch active bans
    cleanup_expired_bans_db()
    c.execute('SELECT user_id, type, reason, timestamp FROM bans')
    rows = c.fetchall()

    emb = red_black_embed("Live Ban List", "")
    if not rows:
        emb.description = "No active bans."
    else:
        for uid, btype, reason, ts in rows:
            try:
                member = await guild.fetch_member(int(uid))
                name = member.display_name
            except Exception:
                name = f"User ID {uid}"
            emb.add_field(name=f"{name} — {btype}", value=f"{reason} ({ts})", inline=False)

    await msg.edit(embed=emb)


# ---------- Embed styling ----------
def red_black_embed(title: str, description: str = None):
    e = discord.Embed(title=title, description=description, color=0x880000)  # dark red
    e.set_footer(text="CPG SGN F1", icon_url=None)
    return e


# ---------- Views ----------
class AttendanceView(discord.ui.View):
    def __init__(self, msg_id: int):
        super().__init__(timeout=None)
        self.msg_id = str(msg_id)

    async def update_embed(self, interaction: Optional[discord.Interaction] = None):
        # Fetch counts & names
        c.execute('SELECT user_id, status FROM attendance WHERE message_id = ?', (self.msg_id,))
        rows = c.fetchall()
        attending = [r[0] for r in rows if r[1] == 'attend']
        not_attend = [r[0] for r in rows if r[1] == 'not']
        maybe = [r[0] for r in rows if r[1] == 'maybe']

        # Build display strings (limit lengths)
        def names_from_ids(guild, ids):
            names = []
            for uid in ids:
                try:
                    m = guild.get_member(int(uid))
                    names.append(m.display_name if m else uid)
                except Exception:
                    names.append(uid)
            return names

        # find the message to edit
        if interaction:
            guild = interaction.guild
            channel = interaction.channel
        else:
            # fallback: can't update without interaction
            return

        # locate message by id in this channel
        try:
            msg = await channel.fetch_message(int(self.msg_id))
        except Exception:
            return

        emb = discord.Embed(title=msg.embeds[0].title if msg.embeds else "Attendance", color=0x880000)
        emb.add_field(name=f"✅ Attending ({len(attending)})", value="\n".join(names_from_ids(guild, attending)) or "None", inline=True)
        emb.add_field(name=f"❌ Not Attending ({len(not_attend)})", value="\n".join(names_from_ids(guild, not_attend)) or "None", inline=True)
        emb.add_field(name=f"🤔 Maybe ({len(maybe)})", value="\n".join(names_from_ids(guild, maybe)) or "None", inline=False)
        try:
            await msg.edit(embed=emb, view=self)
        except Exception:
            pass

    @discord.ui.button(label="Attend ✅", style=discord.ButtonStyle.success, custom_id="attend_yes")
    async def attend(self, interaction: discord.Interaction, button: discord.ui.Button):
        c.execute('REPLACE INTO attendance (message_id, user_id, status, timestamp) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                  (self.msg_id, str(interaction.user.id), "attend"))
        conn.commit()
        await interaction.response.send_message("Marked as attending ✅", ephemeral=True)
        await self.update_embed(interaction)

    @discord.ui.button(label="Not Attend ❌", style=discord.ButtonStyle.danger, custom_id="attend_no")
    async def not_attend(self, interaction: discord.Interaction, button: discord.ui.Button):
        c.execute('REPLACE INTO attendance (message_id, user_id, status, timestamp) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                  (self.msg_id, str(interaction.user.id), "not"))
        conn.commit()
        await interaction.response.send_message("Marked as NOT attending ❌", ephemeral=True)
        await self.update_embed(interaction)

    @discord.ui.button(label="Maybe 🤔", style=discord.ButtonStyle.secondary, custom_id="attend_maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        c.execute('REPLACE INTO attendance (message_id, user_id, status, timestamp) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                  (self.msg_id, str(interaction.user.id), "maybe"))
        conn.commit()
        await interaction.response.send_message("Marked as Maybe 🤔", ephemeral=True)
        await self.update_embed(interaction)

class TicketView(discord.ui.View):
    def __init__(self, guild_id: int, create_msg_title: str = "Create Ticket", create_msg_desc: str = "Click to open a support ticket"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.create_msg_title = create_msg_title
        self.create_msg_desc = create_msg_desc

    @discord.ui.button(label="🎫 Create Ticket", style=discord.ButtonStyle.primary, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user
        # get or create ticket category
        c.execute('SELECT ticket_category_id, steward_role_name FROM settings WHERE guild_id = ?', (str(guild.id),))
        r = c.fetchone()
        category = None
        steward_role_name = r[1] if r and r[1] else DEFAULT_STEWARD_ROLE
        if r and r[0]:
            try:
                category = guild.get_channel(int(r[0]))
            except Exception:
                category = None
        if not category:
            category = await guild.create_category("Tickets")
            c.execute('INSERT OR REPLACE INTO settings (guild_id, ticket_category_id, steward_role_name) VALUES (?, ?, ?)',
                      (str(guild.id), str(category.id), steward_role_name))
            conn.commit()

        # permissions
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            member: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        steward_role = discord.utils.get(guild.roles, name=steward_role_name)
        if steward_role:
            overwrites[steward_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        chan_name = f"ticket-{member.name}".lower().replace(" ", "-")[:90]
        ticket_chan = await guild.create_text_channel(chan_name, category=category, overwrites=overwrites)
        # log
        c.execute('INSERT INTO tickets (guild_id, channel_id, owner_id) VALUES (?, ?, ?)', (str(guild.id), str(ticket_chan.id), str(member.id)))
        conn.commit()

        await interaction.response.send_message(f"Ticket created: {ticket_chan.mention}", ephemeral=True)
        await ticket_chan.send(f"Hello {member.mention}, a steward will be with you shortly. Use `/ticket_close` to close this ticket.")

# ---------- Events ----------
@bot.event
async def on_member_join(member: discord.Member):
    c.execute('SELECT welcome_channel_id, welcome_message FROM settings WHERE guild_id = ?', (str(member.guild.id),))
    r = c.fetchone()
    if r and r[0]:
        ch = member.guild.get_channel(int(r[0]))
        if ch:
            msg_text = r[1] if r[1] else f"Welcome {member.mention} — good luck on track!"
            msg_text = msg_text.replace("{user}", member.mention)
            emb = red_black_embed("Welcome to the league!", msg_text)
            emb.set_thumbnail(url=member.avatar.url if member.avatar else discord.Embed.Empty)
            await ch.send(embed=emb)

@bot.event
async def on_member_remove(member: discord.Member):
    c.execute('SELECT goodbye_channel_id, goodbye_message FROM settings WHERE guild_id = ?', (str(member.guild.id),))
    r = c.fetchone()
    if r and r[0]:
        ch = member.guild.get_channel(int(r[0]))
        if ch:
            msg_text = r[1] if r[1] else f"{member.name} has left the server."
            msg_text = msg_text.replace("{user}", member.name)
            emb = red_black_embed("Goodbye from the league", msg_text)
            await ch.send(embed=emb)

@bot.event
async def on_member_join(member: discord.Member):
    await send_welcome(member)

@bot.event
async def on_member_remove(member: discord.Member):
    await send_goodbye(member)

# ---------- Slash commands: driver / penalties / bans ----------
@tree.command(name="adddriver", description="Add a driver to the database")
@app_commands.describe(user="User to add")
async def adddriver(interaction: discord.Interaction, user: discord.Member):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 You must be a Steward to add drivers.", ephemeral=True); return
    ensure_driver_exists(str(user.id), user.display_name)
    await interaction.response.send_message(f"✅ {user.display_name} added as a driver.", ephemeral=True)

@tree.command(name="removedriver", description="Remove a driver and their data")
@app_commands.describe(user="User to remove")
async def removedriver(interaction: discord.Interaction, user: discord.Member):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward required.", ephemeral=True); return
    c.execute('DELETE FROM drivers WHERE user_id = ?', (str(user.id),))
    c.execute('DELETE FROM penalties WHERE user_id = ?', (str(user.id),))
    c.execute('DELETE FROM bans WHERE user_id = ?', (str(user.id),))
    c.execute('DELETE FROM attendance WHERE user_id = ?', (str(user.id),))
    conn.commit()
    await interaction.response.send_message(f"✅ Removed {user.display_name} and records.", ephemeral=True)

@tree.command(name="drivers", description="List registered drivers")
async def list_drivers(interaction: discord.Interaction):
    c.execute('SELECT name FROM drivers ORDER BY name COLLATE NOCASE')
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No drivers registered.", ephemeral=True); return
    embed = discord.Embed(title="Registered Drivers", description="\n".join(r[0] for r in rows), color=0x880000)
    await interaction.response.send_message(embed=embed)

@tree.command(name="penaltypoints", description="Add penalty points to a driver (Steward only)")
@app_commands.describe(user="Driver", points="Points to add", reason="Reason")
async def penaltypoints(interaction: discord.Interaction, user: discord.Member, points: int, reason: str):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True); return
    ensure_driver_exists(str(user.id), user.display_name)
    c.execute('INSERT INTO penalties (user_id, points, reason) VALUES (?, ?, ?)', (str(user.id), points, reason))
    conn.commit()
    total = update_auto_bans(str(user.id), user.display_name)
    emb = red_black_embed("Penalty Points Added", f"{user.mention} received **{points}** points.\nReason: {reason}")
    emb.add_field(name="Total Points", value=str(total), inline=True)
    # show auto bans active
    c.execute('SELECT type FROM bans WHERE user_id = ?', (str(user.id),))
    active = [r[0] for r in c.fetchall()]
    if active:
        emb.add_field(name="Active Bans", value=", ".join(active), inline=False)
    await interaction.response.send_message(embed=emb)
    await update_live_banlist(interaction.guild)

@tree.command(name="removepoints", description="Remove penalty points from a driver (Steward only)")
@app_commands.describe(user="Driver", points="Points to remove", reason="Reason")
async def removepoints(interaction: discord.Interaction, user: discord.Member, points: int, reason: Optional[str] = "Adjustment"):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True); return
    member_id = str(user.id)
    c.execute('SELECT id, points FROM penalties WHERE user_id = ? ORDER BY timestamp DESC', (member_id,))
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message(f"{user.display_name} has no penalty points.", ephemeral=True); return
    to_remove = points
    removed = 0
    for pid, pts in rows:
        if to_remove <= 0: break
        if pts <= to_remove:
            c.execute('DELETE FROM penalties WHERE id = ?', (pid,))
            removed += pts
            to_remove -= pts
        else:
            new_pts = pts - to_remove
            c.execute('UPDATE penalties SET points = ? WHERE id = ?', (new_pts, pid))
            removed += to_remove
            to_remove = 0
    conn.commit()
    total = update_auto_bans(member_id, user.display_name)
    emb = red_black_embed("Penalty Points Removed", f"Removed **{removed}** points from {user.mention}.\nReason: {reason}")
    emb.add_field(name="Total Points Now", value=str(total), inline=True)
    await interaction.response.send_message(embed=emb)

@tree.command(name="penaltypoints_list", description="Show penalty history & total for a driver")
@app_commands.describe(user="Driver")
async def penaltypoints_list(interaction: discord.Interaction, user: discord.Member):
    c.execute('SELECT points, reason, timestamp FROM penalties WHERE user_id = ? ORDER BY timestamp DESC', (str(user.id),))
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message(f"{user.display_name} has no penalties.", ephemeral=True); return
    total = sum(r[0] for r in rows)
    desc = f"Total Points: **{total}**\n\n"
    for pts, reason, ts in rows:
        desc += f"{ts} — {pts} pts — {reason}\n"
    emb = red_black_embed(f"Penalties for {user.display_name}", desc)
    await interaction.response.send_message(embed=emb)


@tree.command(name="ban", description="Manually apply a ban (Steward only)")
@app_commands.describe(user="Driver", ban_type="race or quali", reason="Reason")
async def ban(interaction: discord.Interaction, user: discord.Member, ban_type: str, reason: Optional[str] = "No reason provided"):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True); return
    btype = ban_type.lower()
    if btype not in ("race", "quali"):
        await interaction.response.send_message("Ban type must be 'race' or 'quali'.", ephemeral=True); return
    ensure_driver_exists(str(user.id), user.display_name)
    c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, ?, ?, CURRENT_TIMESTAMP)', (str(user.id), btype, reason))
    conn.commit()
    await interaction.response.send_message(embed=red_black_embed(f"{btype.title()} Ban Applied", f"{user.mention} banned — {reason}"))
    await update_live_banlist(interaction.guild)

@tree.command(name="remove_ban", description="Remove a ban (Steward only)")
@app_commands.describe(user="Driver", ban_type="race or quali")
async def remove_ban(interaction: discord.Interaction, user: discord.Member, ban_type: str):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True); return
    btype = ban_type.lower()
    if btype not in ("race", "quali"):
        await interaction.response.send_message("Ban type must be 'race' or 'quali'.", ephemeral=True); return
    c.execute('DELETE FROM bans WHERE user_id = ? AND type = ?', (str(user.id), btype))
    conn.commit()
    await interaction.response.send_message(f"✅ Removed {btype} ban from {user.display_name}", ephemeral=True)
    await update_live_banlist(interaction.guild)

@tree.command(name="banlist", description="Show active bans")
async def banlist(interaction: discord.Interaction):
    cleanup_expired_bans_db()
    c.execute('SELECT user_id, type, reason, timestamp FROM bans')
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No active bans.", ephemeral=True); return
    emb = red_black_embed("Active Bans", "")
    for uid, btype, reason, ts in rows:
        try:
            member = await interaction.guild.fetch_member(int(uid))
            name = member.display_name
        except Exception:
            name = f"User ID {uid}"
        emb.add_field(name=f"{name} — {btype}", value=f"{reason} ({ts})", inline=False)
    await interaction.response.send_message(embed=emb)

# ---------- Attendance: create embeds that update live ----------
@tree.command(name="attendance_create", description="Create an attendance embed with live buttons (Steward only)")
@app_commands.describe(channel="Channel to post in", title="Embed title", description="Embed description")
async def attendance_create(interaction: discord.Interaction, channel: discord.TextChannel, title: Optional[str] = "Race Attendance", description: Optional[str] = "Click below to mark attendance."):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True); return
    emb = red_black_embed(title, description)
    view = AttendanceView(msg_id=None)  # we don’t have msg.id yet
    msg = await channel.send(content="@everyone", embed=emb, view=view)  # send once
    view.msg_id = msg.id  # now attach the message id to the view
    view = AttendanceView(msg.id)
    await msg.edit(embed=emb, view=view)
    await interaction.response.send_message(f"✅ Attendance embed posted in {channel.mention}.", ephemeral=True)

# ---------- Ticket setup: customizable embed ----------
class TicketModal(discord.ui.Modal, title="Ticket Button Message"):
    title_field = discord.ui.TextInput(label="Title", placeholder="Ticket header", required=True, max_length=100)
    desc_field = discord.ui.TextInput(label="Description", style=discord.TextStyle.long, placeholder="Message shown above button", required=False, max_length=1000)

    def __init__(self, target_channel: discord.TextChannel):
        super().__init__()
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction):
        emb = red_black_embed(self.title_field.value, self.desc_field.value)
        view = TicketView(interaction.guild.id, create_msg_title=self.title_field.value, create_msg_desc=self.desc_field.value or "Create a ticket")
        await self.target_channel.send(embed=emb, view=view)
        await interaction.response.send_message(f"Ticket message posted in {self.target_channel.mention}", ephemeral=True)

@tree.command(name="ticket_setup", description="Set up ticket creation button (Steward only)")
@app_commands.describe(channel="Channel to post ticket button in")
async def ticket_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True); return
    modal = TicketModal(channel)
    await interaction.response.send_modal(modal)

@tree.command(name="ticket_close", description="Close this ticket (use inside ticket channel)")
async def ticket_close(interaction: discord.Interaction):
    chan = interaction.channel
    c.execute('SELECT id, owner_id FROM tickets WHERE channel_id = ?', (str(chan.id),))
    r = c.fetchone()
    if not r:
        await interaction.response.send_message("This channel is not a ticket.", ephemeral=True); return
    ticket_id, owner_id = r
    c.execute('UPDATE tickets SET closed_at = ? WHERE id = ?', (datetime.datetime.now(), ticket_id))
    conn.commit()
    # Attempt to delete channel
    try:
        await chan.delete(reason=f"Ticket closed by {interaction.user}")
    except Exception:
        await interaction.response.send_message("Could not delete, please remove manually.", ephemeral=True)

# ---------- Welcome / Goodbye setup ----------
class WelcomeModal(discord.ui.Modal, title="Set Welcome Message"):
    message_input = discord.ui.TextInput(
        label="Welcome message",
        style=discord.TextStyle.long,
        placeholder="Use {user} for mention",
        required=True,
        max_length=500
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        # Store in DB
        c.execute('INSERT OR IGNORE INTO settings (guild_id) VALUES (?)', (guild_id,))
        c.execute('UPDATE settings SET welcome_channel_id = ?, welcome_message = ? WHERE guild_id = ?',
                  (str(self.channel.id), self.message_input.value, guild_id))
        conn.commit()
        # Send a preview embed
        text = self.message_input.value.replace("{user}", interaction.user.mention)
        emb = red_black_embed("Welcome Message Preview", text)
        await self.channel.send(embed=emb)
        await interaction.response.send_message(f"✅ Welcome message set and preview sent in {self.channel.mention}", ephemeral=True)

@tree.command(name="welcome_setup", description="Set the channel and message for welcome messages (Steward only)")
@app_commands.describe(channel="Channel to send welcome messages")
async def welcome_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True)
        return
    modal = WelcomeModal(channel)
    await interaction.response.send_modal(modal)


class GoodbyeModal(discord.ui.Modal, title="Set Goodbye Message"):
    message_input = discord.ui.TextInput(
        label="Goodbye message",
        style=discord.TextStyle.long,
        placeholder="Use {user} for name",
        required=True,
        max_length=500
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        c.execute('INSERT OR IGNORE INTO settings (guild_id) VALUES (?)', (guild_id,))
        c.execute('UPDATE settings SET goodbye_channel_id = ?, goodbye_message = ? WHERE guild_id = ?',
                  (str(self.channel.id), self.message_input.value, guild_id))
        conn.commit()
        # Send a preview
        text = self.message_input.value.replace("{user}", interaction.user.mention)
        emb = red_black_embed("Goodbye Message Preview", text)
        await self.channel.send(embed=emb)
        await interaction.response.send_message(f"✅ Goodbye message set and preview sent in {self.channel.mention}", ephemeral=True)

@tree.command(name="goodbye_setup", description="Set the channel and message for goodbye messages (Steward only)")
@app_commands.describe(channel="Channel to send goodbye messages")
async def goodbye_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True)
        return
    modal = GoodbyeModal(channel)
    await interaction.response.send_modal(modal)


@tree.command(name="welcome_edit", description="Edit the welcome message")
@app_commands.describe(message="The new welcome message")
async def welcome_edit(interaction: discord.Interaction, message: str):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    c.execute('INSERT OR IGNORE INTO settings (guild_id) VALUES (?)', (guild_id,))
    c.execute('UPDATE settings SET welcome_message = ? WHERE guild_id = ?', (message, guild_id))
    conn.commit()
    await interaction.response.send_message(f"✅ Welcome message updated. Preview:\n{message}", ephemeral=True)

@tree.command(name="goodbye_edit", description="Edit the goodbye message")
@app_commands.describe(message="The new goodbye message")
async def goodbye_edit(interaction: discord.Interaction, message: str):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    c.execute('INSERT OR IGNORE INTO settings (guild_id) VALUES (?)', (guild_id,))
    c.execute('UPDATE settings SET goodbye_message = ? WHERE guild_id = ?', (message, guild_id))
    conn.commit()
    await interaction.response.send_message(f"✅ Goodbye message updated. Preview:\n{message}", ephemeral=True)


# ---------- Edit welcome/goodbye messages ----------
@tree.command(name="welcome_message", description="Set or edit the welcome message (Steward only)")
@app_commands.describe(message="Message text (use {user} for mention)")
async def welcome_message(interaction: discord.Interaction, message: str):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    c.execute('INSERT OR IGNORE INTO settings (guild_id) VALUES (?)', (guild_id,))
    c.execute('UPDATE settings SET welcome_message = ? WHERE guild_id = ?', (message, guild_id))
    conn.commit()
    await interaction.response.send_message(f"✅ Welcome message updated.", ephemeral=True)


@tree.command(name="goodbye_message", description="Set or edit the goodbye message (Steward only)")
@app_commands.describe(message="Message text (use {user} for name)")
async def goodbye_message(interaction: discord.Interaction, message: str):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    c.execute('INSERT OR IGNORE INTO settings (guild_id) VALUES (?)', (guild_id,))
    c.execute('UPDATE settings SET goodbye_message = ? WHERE guild_id = ?', (message, guild_id))
    conn.commit()
    await interaction.response.send_message(f"✅ Goodbye message updated.", ephemeral=True)


# ---------- Settings: steward role name, ticket log ----------
@tree.command(name="setsystem", description="Set steward role name or ticket log channel (Steward only)")
@app_commands.describe(kind="welcome/goodbye/ticketlog/stewardrole", channel="channel if applicable", value="role name if setting stewardrole")
async def setsystem(interaction: discord.Interaction, kind: str, channel: Optional[discord.TextChannel] = None, value: Optional[str] = None):
    if not is_steward_member(interaction.user):
        await interaction.response.send_message("🚫 Steward only.", ephemeral=True); return
    kind = kind.lower()
    guild_id = str(interaction.guild.id)
    c.execute('INSERT OR IGNORE INTO settings (guild_id) VALUES (?)', (guild_id,))
    if kind == "ticketlog":
        if not channel:
            await interaction.response.send_message("Provide a channel.", ephemeral=True); return
        c.execute('UPDATE settings SET ticket_log_channel_id = ? WHERE guild_id = ?', (str(channel.id), guild_id))
        conn.commit()
        await interaction.response.send_message(f"Ticket log set to {channel.mention}", ephemeral=True)
    elif kind == "stewardrole":
        if not value:
            await interaction.response.send_message("Provide a role name in value.", ephemeral=True); return
        c.execute('UPDATE settings SET steward_role_name = ? WHERE guild_id = ?', (value, guild_id))
        conn.commit()
        await interaction.response.send_message(f"Steward role set to `{value}`", ephemeral=True)
    else:
        await interaction.response.send_message("Valid kinds: ticketlog, stewardrole", ephemeral=True)

# ---------- Background task ----------
@tasks.loop(hours=24)
async def daily_tasks():
    cleanup_expired_bans_db()

# ---------- On ready: sync slash commands ----------
@bot.event
async def on_ready():
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print("Sync error:", e)
    print(f"{bot.user} — online.")
    if not daily_tasks.is_running():
        daily_tasks.start()

# ---------- Run ----------
bot.run(TOKEN)
