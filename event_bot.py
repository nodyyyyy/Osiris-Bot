# event_bot.py — versión integrada (DB persistente + Scheduler + nombres planos + pings fiables + limpieza + imagen)
import discord
from discord.ext import commands
from discord.ui import View, button
import sqlite3
import datetime
import pytz
import json
import asyncio
import os

# --- LOAD CONFIGURATION FROM JSON FILE ---
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise RuntimeError("TOKEN environment variable not set")


# --- CONSTANTS ---
AOO_MAX_ATTENDEES = 30
DB_PATH = "reminders.db"

# 👉 Pon aquí la URL CDN de tu imagen subida a Discord
IMAGE_URL = "https://media.discordapp.net/attachments/1388282858723999914/1435074927240810597/ChatGPT_Image_3_nov_2025_22_05_09.png?format=webp&quality=lossless"

# --- INITIAL SETUP ---
intents = discord.Intents.default()
intents.members = True  # necesario para resolver display_name/global_name
bot = commands.Bot(command_prefix="!", intents=intents)

# --- GLOBALS (DB + LOCK) ---
db_con: sqlite3.Connection | None = None
db_lock = asyncio.Lock()

def setup_database() -> sqlite3.Connection:
    """
    Crea la BD si no existe y devuelve una conexión persistente.
    """
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS aoo_events (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            reminder_time_utc TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    """)
    # Tabla para inscripciones por mensaje (evento)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS aoo_signups (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('accepted','declined')),
            PRIMARY KEY (message_id, user_id)
        )
    """)
    con.commit()
    return con

# --- SCHEDULER DE RECORDATORIOS ---
class ReminderScheduler:
    """
    Scheduler que:
      - Busca el próximo reminder activo (min(reminder_time_utc))
      - Duerme hasta esa hora (o procesa de inmediato si ya venció)
      - Procesa TODOS los vencidos en lote
      - Se despierta anticipadamente si se crea un nuevo evento (notify)
    """
    def __init__(self, bot: commands.Bot, con: sqlite3.Connection, lock: asyncio.Lock):
        self.bot = bot
        self.con = con
        self.lock = lock
        self._task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._stopped = False

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="aoo_reminder_scheduler")

    def stop(self):
        self._stopped = True
        self._wake_event.set()
        if self._task:
            self._task.cancel()

    def notify(self):
        # Llamar esto cuando se inserta un nuevo recordatorio para re-calcular
        self._wake_event.set()

    async def _get_next_reminder_time(self) -> datetime.datetime | None:
        """
        Devuelve el próximo reminder_time_utc (aware UTC) con is_active=1, o None si no hay.
        """
        async with self.lock:
            cur = self.con.cursor()
            cur.execute("""
                SELECT reminder_time_utc
                FROM aoo_events
                WHERE is_active = 1
                ORDER BY reminder_time_utc ASC
                LIMIT 1
            """)
            row = cur.fetchone()
        if not row:
            return None
        # Parseo robusto
        try:
            next_dt = datetime.datetime.fromisoformat(row[0])
        except Exception:
            next_dt = datetime.datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S.%f%z")
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=pytz.utc)
        return next_dt.astimezone(pytz.utc)

    async def _process_due(self):
        """
        Procesa todos los eventos vencidos (reminder_time_utc <= now, is_active=1):
          - Crea thread
          - Menciona asistentes si hay (mensaje normal con <@id>)
          - Marca is_active=0 (para no volver a recordarlo)
          - Programa la eliminación del embed y el hilo al finalizar el evento
        """
        now_utc = datetime.datetime.now(pytz.utc)
        async with self.lock:
            cur = self.con.cursor()
            cur.execute("""
                SELECT message_id, channel_id, reminder_time_utc
                FROM aoo_events
                WHERE is_active = 1 AND reminder_time_utc <= ?
            """, (now_utc.isoformat(),))
            rows = cur.fetchall()

        for message_id, channel_id, reminder_time_utc in rows:
            try:
                channel = await self.bot.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)

                # --- Crear hilo para discusión ---
                title_desc = ""
                if message.embeds:
                    try:
                        title_desc = message.embeds[0].description or ""
                    except Exception:
                        title_desc = ""
                thread_name = f"AoO Discussion & Pings - {title_desc}".strip() or "AoO Discussion & Pings"
                thread = await message.create_thread(name=thread_name)

                # --- Obtener aceptados y enviar recordatorio ---
                async with self.lock:
                    cur2 = self.con.cursor()
                    cur2.execute(
                        "SELECT user_id FROM aoo_signups WHERE message_id = ? AND status = 'accepted'",
                        (message.id,)
                    )
                    accepted_ids = [row[0] for row in cur2.fetchall()]

                if accepted_ids:
                    mentions = " ".join(f"<@{uid}>" for uid in accepted_ids)
                    await thread.send(f"**Reminder!** Ark of Osiris starts in 1 hour!\n\n{mentions}")
                else:
                    await thread.send("**Reminder!** Ark of Osiris starts in 1 hour! No one has signed up yet.")

                # --- Calcular hora exacta del evento (1 hora después del recordatorio) ---
                # reminder_time_utc es ISO de la hora del recordatorio (1h antes). Sumamos 1h.
                base = datetime.datetime.fromisoformat(reminder_time_utc)
                if base.tzinfo is None:
                    base = base.replace(tzinfo=pytz.utc)
                event_time = base + datetime.timedelta(hours=1)

                # --- Programar eliminación de mensaje e hilo ---
                async def cleanup_after_event():
                    now = datetime.datetime.now(pytz.utc)
                    delay = (event_time - now).total_seconds()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    try:
                        await thread.send("The event has concluded. This thread will now be closed.")
                    except:
                        pass
                    try:
                        await message.delete()
                    except Exception as e:
                        print(f"[Cleanup] Could not delete message {message.id}: {e}")
                    try:
                        await thread.delete()
                    except Exception as e:
                        print(f"[Cleanup] Could not delete thread for {message.id}: {e}")
                    print(f"[Cleanup] Event {message.id} and its thread deleted.")

                asyncio.create_task(cleanup_after_event())

            except discord.NotFound:
                print(f"[Scheduler] Message/Channel not found for event {message_id} (deleted?).")
            except Exception as e:
                print(f"[Scheduler] Error sending reminder for {message_id}: {e}")
            finally:
                async with self.lock:
                    cur3 = self.con.cursor()
                    cur3.execute("UPDATE aoo_events SET is_active = 0 WHERE message_id = ?", (message_id,))
                    self.con.commit()

    async def _run(self):
        """
        Bucle principal del scheduler:
          - espera hasta el siguiente reminder (o 30 min si no hay ninguno)
          - permite despertar anticipado con self._wake_event
        """
        while not self._stopped:
            try:
                next_dt = await self._get_next_reminder_time()
                now_utc = datetime.datetime.now(pytz.utc)

                if next_dt is None:
                    # No hay recordatorios: dormir o hasta notify
                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=1800)  # 30 min
                    except asyncio.TimeoutError:
                        pass
                    continue

                # Si ya venció o es ahora, procesar
                if next_dt <= now_utc:
                    await self._process_due()
                    continue

                # Dormir hasta el próximo, pero despertable por notify()
                delay = (next_dt - now_utc).total_seconds()
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    await self._process_due()
                    continue

                # Si fue notify(), recalculamos en el siguiente ciclo
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Scheduler] Unhandled error in loop: {e}")
                await asyncio.sleep(5)

# --- THE VIEW WITH THE EVENT BUTTONS ---
class EventView(View):
    def __init__(self):
        super().__init__(timeout=None)

    async def update_embed(self, interaction: discord.Interaction):
        user = interaction.user
        clicked_button_id = interaction.data['custom_id']  # 'event_attend_btn' o 'event_decline_btn'
        message = interaction.message
        guild = interaction.guild

        # Resolver estado según botón
        status = 'accepted' if clicked_button_id == 'event_attend_btn' else 'declined'

        # (Opcional) Límite de aforo antes de aceptar
        if status == 'accepted':
            async with db_lock:
                cur = db_con.cursor()
                cur.execute("SELECT COUNT(*) FROM aoo_signups WHERE message_id=? AND status='accepted'",
                            (message.id,))
                count = cur.fetchone()[0]
            if count >= AOO_MAX_ATTENDEES:
                return await interaction.response.send_message(
                    f"❌ El evento está lleno ({AOO_MAX_ATTENDEES}).", ephemeral=True
                )

        # 1) Guardar/actualizar status en DB
        async with db_lock:
            cur = db_con.cursor()
            cur.execute("""
                INSERT INTO aoo_signups (message_id, user_id, status)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id, user_id) DO UPDATE SET status=excluded.status
            """, (message.id, user.id, status))
            db_con.commit()

            # 2) Leer todos los inscriptos del mensaje
            cur.execute("SELECT user_id, status FROM aoo_signups WHERE message_id = ?", (message.id,))
            rows = cur.fetchall()

        accepted_ids = [uid for (uid, st) in rows if st == 'accepted']
        declined_ids = [uid for (uid, st) in rows if st == 'declined']

        # 3) Resolver nombres legibles (texto plano) para el embed
        async def id_to_name(uid: int) -> str:
            m = guild.get_member(uid)
            if m is None:
                try:
                    m = await guild.fetch_member(uid)
                except Exception:
                    return f"• ID {uid}"
            name = getattr(m, "global_name", None) or m.display_name
            return f"• {name}"

        accepted_names = [await id_to_name(uid) for uid in accepted_ids]
        declined_names = [await id_to_name(uid) for uid in declined_ids]

        # 4) Reconstruir embed (mismo formato, pero nombres en texto plano) y preservar imagen/footer
        old_embed = interaction.message.embeds[0]
        new_embed = discord.Embed(
            title=old_embed.title,
            description=old_embed.description,
            color=old_embed.color
        )

        # Mantener footer
        if old_embed.footer and old_embed.footer.text:
            new_embed.set_footer(text=old_embed.footer.text)

        # Mantener imagen (para que no desaparezca al editar)
        if old_embed.image and old_embed.image.url:
            new_embed.set_image(url=old_embed.image.url)

        # Campos
        new_embed.add_field(
            name=f"✅ Accepted ({len(accepted_names)}/{AOO_MAX_ATTENDEES})",
            value="\n".join(sorted(accepted_names)) if accepted_names else "\u200b",
            inline=True
        )
        new_embed.add_field(
            name=f"❌ Declined ({len(declined_names)})",
            value="\n".join(sorted(declined_names)) if declined_names else "\u200b",
            inline=True
        )

        await interaction.response.edit_message(embed=new_embed)

    # Botones (mantenemos estilo "secondary"/gris)
    @button(label="✅", style=discord.ButtonStyle.secondary, custom_id="event_attend_btn")
    async def attend_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_embed(interaction)

    @button(label="❌", style=discord.ButtonStyle.secondary, custom_id="event_decline_btn")
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_embed(interaction)

# --- HELPER FUNCTION FOR CREATING POLLS ---
async def create_aoo_poll(interaction: discord.Interaction, target_weekday: int, target_hour: int, scheduler):
    now_utc = datetime.datetime.now(pytz.utc)
    days_ahead = (target_weekday - now_utc.weekday() + 7) % 7
    if days_ahead == 0 and now_utc.hour >= target_hour:
        days_ahead = 7

    event_date = now_utc + datetime.timedelta(days=days_ahead)
    event_datetime_utc = event_date.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    reminder_datetime_utc = event_datetime_utc - datetime.timedelta(hours=1)

    day_name = event_datetime_utc.strftime('%A')
    description = f"{day_name}, {target_hour}:00 UTC"

    # Morado elegante + footer Kingdom 3558 + imagen
    embed = discord.Embed(
        title="Ark of Osiris",
        description=description,
        color=discord.Color.purple()
    )
    embed.add_field(name=f"✅ Accepted (0/{AOO_MAX_ATTENDEES})", value="\u200b", inline=True)
    embed.add_field(name=f"❌ Declined (0)", value="\u200b", inline=True)
    embed.set_footer(text="Kingdom 3558")

    # Imagen por URL CDN
    if IMAGE_URL:
        embed.set_image(url=IMAGE_URL)

    await interaction.response.send_message(embed=embed, view=EventView())
    message = await interaction.original_response()

    async with db_lock:
        cur = db_con.cursor()
        cur.execute(
            "INSERT INTO aoo_events (message_id, channel_id, reminder_time_utc) VALUES (?, ?, ?)",
            (message.id, message.channel.id, reminder_datetime_utc.isoformat())
        )
        db_con.commit()

    # Despertar scheduler (nuevo reminder)
    scheduler.notify()

# --- SLASH COMMANDS (manteniendo tu estructura original) ---
@bot.tree.command(name="aoo_saturday_14", description="Creates the Ark of Osiris poll for Saturday 14:00 UTC.")
async def aoo_saturday_14(interaction: discord.Interaction):
    await create_aoo_poll(interaction, 5, 14, bot.reminder_scheduler)

@bot.tree.command(name="aoo_saturday_20", description="Creates the Ark of Osiris poll for Saturday 20:00 UTC.")
async def aoo_saturday_20(interaction: discord.Interaction):
    await create_aoo_poll(interaction, 5, 20, bot.reminder_scheduler)

@bot.tree.command(name="aoo_sunday_14", description="Creates the Ark of Osiris poll for Sunday 14:00 UTC.")
async def aoo_sunday_14(interaction: discord.Interaction):
    await create_aoo_poll(interaction, 6, 14, bot.reminder_scheduler)

@bot.tree.command(name="aoo_sunday_20", description="Creates the Ark of Osiris poll for Sunday 20:00 UTC.")
async def aoo_sunday_20(interaction: discord.Interaction):
    await create_aoo_poll(interaction, 6, 20, bot.reminder_scheduler)
    
@bot.tree.command(name="aoo_cancel_all", description="Cancela y elimina TODOS los eventos AoO activos.")
async def aoo_cancel_all(interaction: discord.Interaction):
    # 1) Leer todos los eventos activos
    async with db_lock:
        cur = db_con.cursor()
        cur.execute("""
            SELECT message_id, channel_id
            FROM aoo_events
            WHERE is_active = 1
        """)
        rows = cur.fetchall()

    if not rows:
        return await interaction.response.send_message("✅ No hay eventos activos para cancelar.", ephemeral=True)

    total = len(rows)
    deleted_msgs = 0
    cleaned_only = 0

    # 2) Intentar borrar cada mensaje (embed)
    for (mid, ch_id) in rows:
        msg_deleted = False
        try:
            channel = await bot.fetch_channel(ch_id)
            msg = await channel.fetch_message(mid)
            await msg.delete()
            msg_deleted = True
            deleted_msgs += 1
        except discord.NotFound:
            # Mensaje ya no existe; igual limpiaremos en BD
            cleaned_only += 1
        except Exception as e:
            # No se pudo borrar el mensaje (permiso, etc). Igual limpiamos en BD.
            print(f"[CancelAll] Error deleting message {mid}: {e}")
            cleaned_only += 1

        # 3) Limpiar inscripciones y fila del evento
        async with db_lock:
            cur = db_con.cursor()
            cur.execute("DELETE FROM aoo_signups WHERE message_id = ?", (mid,))
            cur.execute("DELETE FROM aoo_events WHERE message_id = ?", (mid,))
            db_con.commit()

    # 4) Respuesta resumida
    msg = (
        f"🧹 **Cancelación completa de eventos activos**\n"
        f"• Total detectados: **{total}**\n"
        f"• Embeds eliminados: **{deleted_msgs}**\n"
        f"• Sólo limpieza en BD (mensaje ya no estaba / sin permisos): **{cleaned_only}**"
    )
    await interaction.response.send_message(msg, ephemeral=True)
# --- BOT EVENTS ---
@bot.event
async def on_ready():
    global db_con
    db_con = setup_database()
    bot.add_view(EventView())

    # Iniciar scheduler con la conexión persistente
    bot.reminder_scheduler = ReminderScheduler(bot, db_con, db_lock)
    bot.reminder_scheduler.start()

    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    print(f'Bot connected as {bot.user}!')
    print('-----------------------------------------')

# --- RUN THE BOT ---
try:
    bot.run(TOKEN)
finally:
    # Cierre limpio de la DB al apagar
    try:
        if db_con:
            db_con.close()
    except Exception:
        pass
