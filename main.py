# main.py
import os
import discord
from discord.ext import commands
import sqlite3
import datetime
from flask import Flask
from threading import Thread

# ----------------------------
# Flask keep-alive (for Replit/Uptime)
# ----------------------------
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_web).start()

# ----------------------------
# Bot setup
# ----------------------------
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable! Set TOKEN in your environment/secrets.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # needed to resolve Member converters
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------------------
# Database (SQLite)
# ----------------------------
conn = sqlite3.connect('drivers.db', check_same_thread=False)
c = conn.cursor()

c.execute('''
CREATE TABLE IF NOT EXISTS drivers (
    user_id TEXT PRIMARY KEY,
    name TEXT
)
''')

c.execute('''
CREATE TABLE IF NOT EXISTS penalties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    points INTEGER,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
''')

c.execute('''
CREATE TABLE IF NOT EXISTS bans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    type TEXT,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
''')
conn.commit()

# ----------------------------
# Helpers / Staff check
# ----------------------------
def is_steward():
    async def predicate(ctx):
        role = discord.utils.get(ctx.author.roles, name="Steward")
        if role is None:
            await ctx.send("❌ You must have the `Steward` role to use this command.")
            return False
        return True
    return commands.check(predicate)

def ensure_driver_exists(member_id: str, name: str):
    c.execute('INSERT OR IGNORE INTO drivers (user_id, name) VALUES (?, ?)', (member_id, name))
    conn.commit()

def update_automatic_bans_from_id(member_id_str: str, member_name: str = None):
    """
    Ensures driver exists, calculates total points and inserts/removes automatic bans:
      - quali at >=10
      - race at >=15
    Returns total_points (int)
    """
    # ensure driver row exists (name optional)
    if member_name:
        ensure_driver_exists(member_id_str, member_name)
    else:
        c.execute('INSERT OR IGNORE INTO drivers (user_id, name) VALUES (?, ?)', (member_id_str, member_id_str))
        conn.commit()

    # compute total points
    c.execute('SELECT SUM(points) FROM penalties WHERE user_id = ?', (member_id_str,))
    total_points = c.fetchone()[0] or 0

    # Quali ban if >=10
    c.execute('SELECT id FROM bans WHERE user_id = ? AND type = "quali"', (member_id_str,))
    has_quali = c.fetchone() is not None
    if total_points >= 10 and not has_quali:
        c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "quali", ?, CURRENT_TIMESTAMP)',
                  (member_id_str, "Automatic quali ban for reaching 10+ penalty points"))
    elif total_points < 10 and has_quali:
        c.execute('DELETE FROM bans WHERE user_id = ? AND type = "quali"', (member_id_str,))

    # Race ban if >=15
    c.execute('SELECT id FROM bans WHERE user_id = ? AND type = "race"', (member_id_str,))
    has_race = c.fetchone() is not None
    if total_points >= 15 and not has_race:
        c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "race", ?, CURRENT_TIMESTAMP)',
                  (member_id_str, "Automatic race ban for reaching 15+ penalty points"))
    elif total_points < 15 and has_race:
        c.execute('DELETE FROM bans WHERE user_id = ? AND type = "race"', (member_id_str,))

    conn.commit()
    return total_points

def cleanup_expired_bans():
    """Deletes bans older than 8 days"""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=8)
    # SQLite timestamp string stored as 'YYYY-MM-DD HH:MM:SS' by CURRENT_TIMESTAMP
    c.execute('SELECT id, timestamp FROM bans')
    rows = c.fetchall()
    for ban_id, ts in rows:
        try:
            ban_time = datetime.datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        except Exception:
            # try ISO parse fallback
            try:
                ban_time = datetime.datetime.fromisoformat(ts)
            except Exception:
                ban_time = None
        if ban_time and ban_time < cutoff:
            c.execute('DELETE FROM bans WHERE id = ?', (ban_id,))
    conn.commit()

# ----------------------------
# Events
# ----------------------------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online and ready!")

# ----------------------------
# Commands
# ----------------------------
@bot.command()
async def ping(ctx):
    await ctx.send(embed=discord.Embed(title="Pong 🏁", description="Bot is online", color=0x00ff00))

# Add driver
@bot.command()
async def adddriver(ctx, member: discord.Member):
    ensure_driver_exists(str(member.id), member.name)
    embed = discord.Embed(title="Driver Added ✅", description=f"{member.name} added to drivers list.", color=0x00ff00)
    await ctx.send(embed=embed)

# List drivers
@bot.command()
async def drivers(ctx):
    c.execute('SELECT name FROM drivers ORDER BY name COLLATE NOCASE')
    rows = c.fetchall()
    if not rows:
        embed = discord.Embed(title="Drivers", description="No drivers registered.", color=0xffcc00)
    else:
        names = "\n".join(r[0] for r in rows)
        embed = discord.Embed(title="Registered Drivers 🏎️", description=names, color=0x00ff00)
    await ctx.send(embed=embed)

# Add penalty (staff only)
@bot.command()
@is_steward()
async def penalty(ctx, member: discord.Member, points: int, *, reason: str = "No reason provided"):
    member_id = str(member.id)
    ensure_driver_exists(member_id, member.name)

    c.execute('INSERT INTO penalties (user_id, points, reason) VALUES (?, ?, ?)', (member_id, points, reason))
    conn.commit()

    total_points = update_automatic_bans_from_id(member_id, member.name)

    embed = discord.Embed(title="Penalty Added ⚠️", color=0xff9900)
    embed.add_field(name="Driver", value=member.name, inline=True)
    embed.add_field(name="Points", value=str(points), inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Points", value=str(total_points), inline=True)

    # Show which automatic bans are active now
    c.execute('SELECT type FROM bans WHERE user_id = ?', (member_id,))
    active = [r[0] for r in c.fetchall()]
    if active:
        embed.add_field(name="Active Bans", value=", ".join(active), inline=False)

    await ctx.send(embed=embed)

# List penalties for member
@bot.command()
async def penalties(ctx, member: discord.Member):
    member_id = str(member.id)
    c.execute('SELECT points, reason, timestamp FROM penalties WHERE user_id = ? ORDER BY timestamp DESC', (member_id,))
    rows = c.fetchall()
    if not rows:
        embed = discord.Embed(title="Penalties", description=f"{member.name} has no penalties.", color=0x00ff00)
    else:
        total = sum(r[0] for r in rows)
        desc = f"Total Points: **{total}**\n\n"
        for pts, reason, ts in rows:
            desc += f"{ts} — {pts} pts — {reason}\n"
        embed = discord.Embed(title=f"Penalties for {member.name} ⚠️", description=desc, color=0xff9900)
    await ctx.send(embed=embed)

# Manual race ban
@bot.command()
@is_steward()
async def banrace(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    member_id = str(member.id)
    ensure_driver_exists(member_id, member.name)
    c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "race", ?, CURRENT_TIMESTAMP)', (member_id, reason))
    conn.commit()
    embed = discord.Embed(title="Race Ban ⛔", description=f"{member.name} has been banned from the next race.", color=0xff0000)
    embed.add_field(name="Reason", value=reason, inline=False)
    await ctx.send(embed=embed)

# Manual quali ban
@bot.command()
@is_steward()
async def banquali(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    member_id = str(member.id)
    ensure_driver_exists(member_id, member.name)
    c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "quali", ?, CURRENT_TIMESTAMP)', (member_id, reason))
    conn.commit()
    embed = discord.Embed(title="Qualifying Ban ⛔", description=f"{member.name} has been banned from the next qualifying.", color=0xff0000)
    embed.add_field(name="Reason", value=reason, inline=False)
    await ctx.send(embed=embed)

# Show active bans (also cleans expired)
@bot.command()
async def bans(ctx):
    cleanup_expired_bans()
    c.execute('SELECT d.name, b.type, b.reason, b.timestamp FROM bans b JOIN drivers d ON b.user_id = d.user_id ORDER BY b.timestamp')
    rows = c.fetchall()
    if not rows:
        embed = discord.Embed(title="Bans", description="No active bans.", color=0x00ff00)
    else:
        desc = ""
        for name, btype, reason, ts in rows:
            desc += f"**{name}** — {btype} — {reason} ({ts})\n"
        embed = discord.Embed(title="Active Bans 🏁", description=desc, color=0xff0000)
    await ctx.send(embed=embed)

# Remove a specific ban
@bot.command()
@is_steward()
async def removeban(ctx, member: discord.Member, ban_type: str):
    ban_type = ban_type.lower()
    if ban_type not in ("race", "quali"):
        await ctx.send("Ban type must be 'race' or 'quali'.")
        return
    member_id = str(member.id)
    c.execute('DELETE FROM bans WHERE user_id = ? AND type = ?', (member_id, ban_type))
    conn.commit()
    embed = discord.Embed(title="Ban Removed ✅", description=f"{member.name}'s {ban_type} ban removed.", color=0x00ff00)
    await ctx.send(embed=embed)

# Remove penalty points (staff only)
@bot.command()
@is_steward()
async def removepenalty(ctx, member: discord.Member, points: int, *, reason: str = "Adjustment"):
    member_id = str(member.id)
    # fetch penalties newest-first
    c.execute('SELECT id, points FROM penalties WHERE user_id = ? ORDER BY timestamp DESC', (member_id,))
    penalties = c.fetchall()
    if not penalties:
        await ctx.send(f"{member.name} has no penalty points to remove.")
        return

    to_remove = points
    removed = 0
    for pid, p_points in penalties:
        if to_remove <= 0:
            break
        if p_points <= to_remove:
            c.execute('DELETE FROM penalties WHERE id = ?', (pid,))
            removed += p_points
            to_remove -= p_points
        else:
            new_points = p_points - to_remove
            c.execute('UPDATE penalties SET points = ? WHERE id = ?', (new_points, pid))
            removed += to_remove
            to_remove = 0
    conn.commit()

    # recalculates and update automatic bans
    total_points = update_automatic_bans_from_id(member_id, member.name)

    embed = discord.Embed(title="Penalty Points Removed ✅", color=0x00ff00)
    embed.add_field(name="Driver", value=member.name, inline=True)
    embed.add_field(name="Points Removed", value=str(removed), inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Points Now", value=str(total_points), inline=True)
    await ctx.send(embed=embed)

# ----------------------------
# Run bot
# ----------------------------
bot.run(TOKEN)

