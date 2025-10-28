# main.py
import os
import discord
from discord.ext import commands
import sqlite3
import datetime
from flask import Flask
from threading import Thread

# ----- Flask keep-alive -----
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run).start()

# ----- Bot setup -----
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable!")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ----- Database -----
conn = sqlite3.connect('drivers.db', check_same_thread=False)
c = conn.cursor()

# Drivers table
c.execute('''
CREATE TABLE IF NOT EXISTS drivers (
    user_id TEXT PRIMARY KEY,
    name TEXT
)
''')
# Penalties table
c.execute('''
CREATE TABLE IF NOT EXISTS penalties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    points INTEGER,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
''')
# Bans table
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

# ----- Staff check -----
def is_steward():
    async def predicate(ctx):
        role = discord.utils.get(ctx.author.roles, name="Steward")
        if role is None:
            await ctx.send("❌ You must have the `Steward` role to use this command.")
            return False
        return True
    return commands.check(predicate)

# ----- Automatic ban updater -----
def update_automatic_bans(member: discord.Member):
    member_id = str(member.id)

    # Ensure driver exists
    c.execute('INSERT OR IGNORE INTO drivers (user_id, name) VALUES (?, ?)', (member_id, member.name))

    # Total penalty points
    c.execute('SELECT SUM(points) FROM penalties WHERE user_id = ?', (member_id,))
    total_points = c.fetchone()[0] or 0

    # Automatic Qualifying Ban
    c.execute('SELECT * FROM bans WHERE user_id = ? AND type = "quali"', (member_id,))
    if total_points >= 10 and not c.fetchone():
        c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "quali", ?, CURRENT_TIMESTAMP)',
                  (member_id, "Automatic quali ban for 10+ points"))

    # Automatic Race Ban
    c.execute('SELECT * FROM bans WHERE user_id = ? AND type = "race"', (member_id,))
    if total_points >= 15 and not c.fetchone():
        c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "race", ?, CURRENT_TIMESTAMP)',
                  (member_id, "Automatic race ban for 15+ points"))

    # Remove bans if points drop below thresholds
    c.execute('SELECT * FROM bans WHERE user_id = ? AND type = "quali"', (member_id,))
    if total_points < 10 and c.fetchone():
        c.execute('DELETE FROM bans WHERE user_id = ? AND type = "quali"', (member_id,))

    c.execute('SELECT * FROM bans WHERE user_id = ? AND type = "race"', (member_id,))
    if total_points < 15 and c.fetchone():
        c.execute('DELETE FROM bans WHERE user_id = ? AND type = "race"', (member_id,))

    conn.commit()
    return total_points

# ----- Events -----
@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online and ready!")

# ----- Driver commands -----
@bot.command()
async def adddriver(ctx, member: discord.Member):
    c.execute('INSERT OR IGNORE INTO drivers (user_id, name) VALUES (?, ?)', (str(member.id), member.name))
    conn.commit()
    embed = discord.Embed(title="Driver Added ✅", description=f"{member.name} has been added as a driver.", color=0x00ff00)
    await ctx.send(embed=embed)

@bot.command()
async def drivers(ctx):
    c.execute('SELECT name FROM drivers')
    all_drivers = c.fetchall()
    if not all_drivers:
        embed = discord.Embed(title="Drivers", description="No drivers registered yet.", color=0xffcc00)
    else:
        driver_list = "\n".join([d[0] for d in all_drivers])
        embed = discord.Embed(title="Registered Drivers 🏎️", description=driver_list, color=0x00ff00)
    await ctx.send(embed=embed)

# ----- Penalty commands -----
@bot.command()
@is_steward()
async def penalty(ctx, member: discord.Member, points: int, *, reason: str = "No reason provided"):
    # Ensure driver exists
    c.execute('INSERT OR IGNORE INTO drivers (user_id, name) VALUES (?, ?)', (str(member.id), member.name))
    conn.commit()

    # Add penalty
    c.execute('INSERT INTO penalties (user_id, points, reason) VALUES (?, ?, ?)', (str(member.id), points, reason))
    conn.commit()

    # Update automatic bans
    total_points = update_automatic_bans(member)

    embed = discord.Embed(title="Penalty Added ⚠️", color=0xff9900)
    embed.add_field(name="Driver", value=member.name, inline=True)
    embed.add_field(name="Points", value=points, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Points", value=total_points, inline=True)

    # Show automatic bans applied
    bans_applied = []
    c.execute('SELECT type FROM bans WHERE user_id = ?', (str(member.id),))
    active_bans = [row[0] for row in c.fetchall()]
    if "quali" in active_bans and total_points >= 10:
        bans_applied.append("⛔ Qualifying ban applied")
    if "race" in active_bans and total_points >= 15:
        bans_applied.append("⛔ Race ban applied")
    if bans_applied:
        embed.add_field(name="Automatic Bans", value="\n".join(bans_applied), inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def penalties(ctx, member: discord.Member):
    c.execute('SELECT points, reason, timestamp FROM penalties WHERE user_id = ?', (str(member.id),))
    entries = c.fetchall()
    if not entries:
        embed = discord.Embed(title="Penalties", description=f"{member.name} has no penalty points.", color=0x00ff00)
    else:
        total = sum([p[0] for p in entries])
        description = f"Total Points: {total}\n\n"
        for p in entries:
            description += f"{p[2]} — {p[0]} pts — {p[1]}\n"
        embed = discord.Embed(title=f"Penalties for {member.name} ⚠️", description=description, color=0xff9900)
    await ctx.send(embed=embed)

# ----- Ban commands -----
@bot.command()
@is_steward()
async def banrace(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    # Ensure driver exists
    c.execute('INSERT OR IGNORE INTO drivers (user_id, name) VALUES (?, ?)', (str(member.id), member.name))
    conn.commit()

    # Add race ban
    c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "race", ?, CURRENT_TIMESTAMP)',
              (str(member.id), reason))
    conn.commit()

    embed = discord.Embed(title="Race Ban ⛔", description=f"{member.name} has been banned from the next race.", color=0xff0000)
    embed.add_field(name="Reason", value=reason, inline=False)
    await ctx.send(embed=embed)

@bot.command()
@is_steward()
async def banquali(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    # Ensure driver exists
    c.execute('INSERT OR IGNORE INTO drivers (user_id, name) VALUES (?, ?)', (str(member.id), member.name))
    conn.commit()

    # Add quali ban
    c.execute('INSERT INTO bans (user_id, type, reason, timestamp) VALUES (?, "quali", ?, CURRENT_TIMESTAMP)',
              (str(member.id), reason))
    conn.commit()

    embed = discord.Embed(title="Qualifying Ban ⛔", description=f"{member.name} has been banned from the next qualifying.", color=0xff0000)
    embed.add_field(name="Reason", value=reason, inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def bans(ctx):
    # Remove bans older than 8 days
    c.execute('SELECT id, timestamp FROM bans')
    all_bans = c.fetchall()
    now = datetime.datetime.now()
    for ban_id, ts_str in all_bans:
        try:
            ban_time = datetime.datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
        except Exception:
            ban_time = datetime.datetime.fromisoformat(ts_str)
        if now - ban_time > datetime.timedelta(days=8):
            c.execute('DELETE FROM bans WHERE id = ?', (ban_id,))
    conn.commit()

    # Show active bans
    c.execute('SELECT d.name, b.type, b.reason, b.timestamp FROM bans b JOIN drivers d ON b.user_id = d.user_id')
    entries = c.fetchall()
    if not entries:
        embed = discord.Embed(title="Bans", description="No active bans.", color=0x00ff00)
    else:
        description = ""
        for e in entries:
            description += f"{e[0]} — {e[1]} — {e[2]} ({e[3]})\n"
        embed = discord.Embed(title="Active Bans 🏁", description=description, color=0xff0000)
    await ctx.send(embed=embed)

@bot.command()
@is_steward()
async def removeban(ctx, member: discord.Member, ban_type: str):
    if ban_type.lower() not in ["race", "quali"]:
        await ctx.send("Ban type must be 'race' or 'quali'.")
        return
    c.execute('DELETE FROM bans WHERE user_id = ? AND type = ?', (str(member.id), ban_type.lower()))
    conn.commit()
    embed = discord.Embed(title="Ban Removed ✅", description=f"{member.name}'s {ban_type} ban has been removed.", color=0x00ff00)
    await ctx.send(embed=embed)

@bot.command()
@is_steward()
async def removepenalty(ctx, member: discord.Member, points: int, *, reason: str = "Adjustment"):
    # Get all penalties
    c.execute('SELECT id, points FROM penalties WHERE user_id = ? ORDER BY timestamp DESC', (str(member.id),))
    penalties = c.fetchall()
    if not penalties:
        await ctx.send(f"{member.name} has no penalty points to remove.")
        return

    to_remove = points
    removed_points = 0

    for pid, p_points in penalties:
        if to_remove <= 0:
            break
        if p_points <= to_remove:
            c.execute('DELETE FROM penalties WHERE id = ?', (pid,))
            removed_points += p_points
            to_remove -= p_points
        else:
            new_points = p_points - to_remove
            c.execute('UPDATE penalties SET points = ? WHERE id = ?', (new_points, pid))
            removed_points += to_remove
            to_remove = 0
    conn.commit()

    # Update automatic bans
    total_points = update_automatic_bans(member)

    embed = discord.Embed(title="Penalty Points Removed ✅", color=0x00ff00)
    embed.add_field(name="Driver", value=member.name, inline=True)
    embed.add_field(name="Points Removed", value=removed_points, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Points Now", value=total_points, inline=True)
    await ctx.send(embed=embed)

# ----- Run bot -----
bot.run(TOKEN)


