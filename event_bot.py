# ================= IMPORTS =================
import discord
from discord.ext import commands
from discord.ui import View, button
import aiosqlite
import datetime
import pytz
import asyncio
from flask import Flask
import os
import threading

# ================= WEB SERVER =================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def start_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=start_web, daemon=True).start()

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN environment variable not set")

AOO_MAX_ATTENDEES = 30
DB_PATH = "reminders.db"

IMAGE_URL = "https://media.discordapp.net/attachments/1388282858723999914/1435074927240810597/ChatGPT_Image_3_nov_2025_22_05_09.png?format=webp&quality=lossless"

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_con: aiosqlite.Connection | None = None
db_lock = asyncio.Lock()

# ================= DATABASE =================
async def setup_database():
    con = await aiosqlite.connect(DB_PATH)

    await con.execute("""
        CREATE TABLE IF NOT EXISTS aoo_events (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            reminder_time_utc TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    """)

    await con.execute("""
        CREATE TABLE IF NOT EXISTS aoo_signups (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('accepted','declined')),
            PRIMARY KEY (message_id, user_id)
        )
    """)

    await con.commit()
    return con

# ================= SCHEDULER =================
class ReminderScheduler:
    def __init__(self, bot, con, lock):
        self.bot = bot
        self.con = con
        self.lock = lock
        self._task = None

    def start(self):
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            await asyncio.sleep(30)
            await self._process_due()

    async def _process_due(self):
        now_utc = datetime.datetime.now(pytz.utc)

        async with self.lock:
            async with self.con.execute("""
                SELECT message_id, channel_id, reminder_time_utc
                FROM aoo_events
                WHERE is_active = 1 AND reminder_time_utc <= ?
            """, (now_utc.isoformat(),)) as cursor:
                events = await cursor.fetchall()

        for message_id, channel_id, reminder_time_utc in events:
            try:
                channel = await self.bot.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)

                thread = await message.create_thread(
                    name="AoO Discussion & Pings"
                )

                async with self.lock:
                    async with self.con.execute(
                        "SELECT user_id FROM aoo_signups WHERE message_id=? AND status='accepted'",
                        (message_id,)
                    ) as cursor:
                        accepted = [r[0] for r in await cursor.fetchall()]

                if accepted:
                    mentions = " ".join(f"<@{uid}>" for uid in accepted)
                    await thread.send(
                        f"**Reminder!** Ark of Osiris starts now!\n\n{mentions}"
                    )
                else:
                    await thread.send(
                        "**Reminder!** Ark of Osiris starts now!"
                    )

                # Marcar como procesado
                await self.con.execute(
                    "UPDATE aoo_events SET is_active=0 WHERE message_id=?",
                    (message_id,)
                )
                await self.con.commit()

                # Cleanup 2h después
                asyncio.create_task(
                    self.cleanup_event(message, thread, message_id)
                )

            except Exception as e:
                print(f"[Scheduler Error] {e}")

    async def cleanup_event(self, message, thread, message_id):
        await asyncio.sleep(7200)

        async with self.lock:
            await self.con.execute(
                "DELETE FROM aoo_signups WHERE message_id=?",
                (message_id,)
            )
            await self.con.execute(
                "DELETE FROM aoo_events WHERE message_id=?",
                (message_id,)
            )
            await self.con.commit()

        try:
            await message.delete()
        except:
            pass

        try:
            await thread.delete()
        except:
            pass

# ================= VIEW =================
class EventView(View):
    def __init__(self):
        super().__init__(timeout=None)

    async def update_embed(self, interaction: discord.Interaction):
        user = interaction.user
        message = interaction.message
        status = 'accepted' if interaction.data['custom_id'] == 'event_attend_btn' else 'declined'

        async with db_lock:
            await db_con.execute("""
                INSERT INTO aoo_signups (message_id, user_id, status)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id, user_id)
                DO UPDATE SET status=excluded.status
            """, (message.id, user.id, status))
            await db_con.commit()

            async with db_con.execute(
                "SELECT user_id, status FROM aoo_signups WHERE message_id=?",
                (message.id,)
            ) as cursor:
                rows = await cursor.fetchall()

        accepted = [r[0] for r in rows if r[1] == 'accepted']
        declined = [r[0] for r in rows if r[1] == 'declined']

        embed = interaction.message.embeds[0]
        new_embed = discord.Embed(
            title=embed.title,
            description=embed.description,
            color=embed.color
        )

        new_embed.set_footer(text="Kingdom 3558")
        new_embed.set_image(url=IMAGE_URL)

        new_embed.add_field(
            name=f"✅ Accepted ({len(accepted)}/{AOO_MAX_ATTENDEES})",
            value="\n".join(f"<@{u}>" for u in accepted) if accepted else "\u200b",
            inline=True
        )

        new_embed.add_field(
            name=f"❌ Declined ({len(declined)})",
            value="\n".join(f"<@{u}>" for u in declined) if declined else "\u200b",
            inline=True
        )

        await interaction.response.edit_message(embed=new_embed)

    @button(label="✅", style=discord.ButtonStyle.secondary, custom_id="event_attend_btn")
    async def attend(self, interaction: discord.Interaction, button):
        await self.update_embed(interaction)

    @button(label="❌", style=discord.ButtonStyle.secondary, custom_id="event_decline_btn")
    async def decline(self, interaction: discord.Interaction, button):
        await self.update_embed(interaction)

# ================= CREATE EVENT =================
async def create_aoo_poll(interaction, weekday, hour):
    now = datetime.datetime.now(pytz.utc)
    days = (weekday - now.weekday() + 7) % 7

    event_dt = now + datetime.timedelta(days=days)
    event_dt = event_dt.replace(hour=hour, minute=0, second=0, microsecond=0)

    reminder_dt = event_dt - datetime.timedelta(hours=1)

    embed = discord.Embed(
        title="Ark of Osiris",
        description=f"{event_dt.strftime('%A')} {hour}:00 UTC",
        color=discord.Color.purple()
    )

    embed.set_footer(text="Kingdom 3558")
    embed.set_image(url=IMAGE_URL)

    await interaction.response.send_message(embed=embed, view=EventView())
    message = await interaction.original_response()

    async with db_lock:
        await db_con.execute(
            "INSERT INTO aoo_events (message_id, channel_id, reminder_time_utc) VALUES (?, ?, ?)",
            (message.id, message.channel.id, reminder_dt.isoformat())
        )
        await db_con.commit()

# ================= SLASH COMMANDS =================
@bot.tree.command(name="aoo_saturday_14")
async def aoo_saturday_14(interaction: discord.Interaction):
    await create_aoo_poll(interaction, 5, 14)

@bot.tree.command(name="aoo_saturday_20")
async def aoo_saturday_20(interaction: discord.Interaction):
    await create_aoo_poll(interaction, 5, 20)

@bot.tree.command(name="aoo_sunday_14")
async def aoo_sunday_14(interaction: discord.Interaction):
    await create_aoo_poll(interaction, 6, 14)

@bot.tree.command(name="aoo_sunday_20")
async def aoo_sunday_20(interaction: discord.Interaction):
    await create_aoo_poll(interaction, 6, 20)

@bot.tree.command(name="aoo_status")
async def aoo_status(interaction: discord.Interaction):
    async with db_lock:
        async with db_con.execute("""
            SELECT message_id, reminder_time_utc
            FROM aoo_events
            WHERE is_active=1
        """) as cursor:
            events = await cursor.fetchall()

    if not events:
        return await interaction.response.send_message(
            "📭 No active events.",
            ephemeral=True
        )

    desc = ""
    for message_id, reminder_time in events:
        event_time = datetime.datetime.fromisoformat(reminder_time) + datetime.timedelta(hours=1)
        desc += f"🆔 `{message_id}` - {event_time.strftime('%A %H:%M UTC')}\n"

    embed = discord.Embed(
        title="Active AoO Events",
        description=desc,
        color=discord.Color.purple()
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="aoo_cancel_all")
async def aoo_cancel_all(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "❌ Admin only.",
            ephemeral=True
        )

    async with db_lock:
        await db_con.execute("DELETE FROM aoo_signups")
        await db_con.execute("DELETE FROM aoo_events")
        await db_con.commit()

    await interaction.response.send_message(
        "🔥 All events deleted.",
        ephemeral=True
    )

# ================= RESTORE =================
async def restore_active_events():
    async with db_lock:
        async with db_con.execute(
            "SELECT message_id, channel_id FROM aoo_events WHERE is_active=1"
        ) as cursor:
            events = await cursor.fetchall()

    for message_id, channel_id in events:
        try:
            channel = await bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            await message.edit(view=EventView())
        except:
            pass

# ================= READY =================
@bot.event
async def on_ready():
    global db_con
    db_con = await setup_database()

    bot.add_view(EventView())
    await restore_active_events()

    bot.scheduler = ReminderScheduler(bot, db_con, db_lock)
    bot.scheduler.start()

    await bot.tree.sync()
    print(f"Bot connected as {bot.user}")

# ================= RUN =================
bot.run(TOKEN)
