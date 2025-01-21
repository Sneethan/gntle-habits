import discord
from discord import app_commands
from datetime import datetime, timedelta
import random
import aiosqlite

class HabitCommands(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="habit", description="Gentle habit tracking commands")
        self.bot = bot

    @app_commands.command(name="create", description="Create a new habit to track")
    @app_commands.describe(
        name="Name of the habit",
        reminder_time="Time for daily reminder (HH:MM in 24h format)",
        expiry_time="Time when the reminder expires (HH:MM in 24h format)",
        description="Optional description of the habit"
    )
    async def create_habit(
        self,
        interaction: discord.Interaction,
        name: str,
        reminder_time: str,
        expiry_time: str,
        description: str = None
    ):
        # Validate time formats
        try:
            datetime.strptime(reminder_time, "%H:%M")
            datetime.strptime(expiry_time, "%H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Please use HH:MM format for times (e.g., 09:00, 14:30)",
                ephemeral=True
            )
            return
        
        async with aiosqlite.connect(self.bot.db_path) as db:
            try:
                await db.execute(
                    '''INSERT INTO habits 
                       (name, reminder_time, expiry_time, description, created_at)
                       VALUES (?, ?, ?, ?, ?)''',
                    (name, reminder_time, expiry_time, description, datetime.now().isoformat())
                )
                await db.commit()
                
                # Restart scheduler to include new habit
                await self.bot.setup_scheduler()
                
                await interaction.response.send_message(
                    f"✨ Created new habit: {name}\n"
                    f"Daily reminder at: {reminder_time}\n"
                    f"Expires at: {expiry_time}",
                    ephemeral=True
                )
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(
                    f"A habit with the name '{name}' already exists!",
                    ephemeral=True
                )

    @app_commands.command(name="list", description="List all your habits and streaks")
    async def list_habits(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.bot.db_path) as db:
            cursor = await db.execute('''
                SELECT h.name, h.reminder_time, h.description, 
                       COALESCE(uh.current_streak, 0) as streak
                FROM habits h
                LEFT JOIN user_habits uh 
                    ON h.id = uh.habit_id 
                    AND uh.user_id = ?
                ORDER BY h.created_at
            ''', (interaction.user.id,))
            
            habits = await cursor.fetchall()
            
            if not habits:
                await interaction.response.send_message(
                    "No habits have been created yet! Use `/habit create` to get started.",
                    ephemeral=True
                )
                return
            
            embed = discord.Embed(
                title="Your Habits",
                description="Here are all your tracked habits:",
                color=discord.Color.blue()
            )
            
            for name, reminder_time, description, streak in habits:
                value = f"⏰ Reminder: {reminder_time}\n🔥 Current streak: {streak}"
                if description:
                    value += f"\n📝 {description}"
                embed.add_field(
                    name=name,
                    value=value,
                    inline=False
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="delete", description="Delete a habit")
    @app_commands.describe(name="Name of the habit to delete")
    async def delete_habit(self, interaction: discord.Interaction, name: str):
        async with aiosqlite.connect(self.bot.db_path) as db:
            cursor = await db.execute('SELECT id FROM habits WHERE name = ?', (name,))
            habit = await cursor.fetchone()
            
            if not habit:
                await interaction.response.send_message(
                    f"Could not find a habit named '{name}'",
                    ephemeral=True
                )
                return
            
            habit_id = habit[0]
            
            # Delete the habit and all associated user data
            await db.execute('DELETE FROM user_habits WHERE habit_id = ?', (habit_id,))
            await db.execute('DELETE FROM habits WHERE id = ?', (habit_id,))
            await db.commit()
            
            # Restart scheduler to remove deleted habit
            await self.bot.setup_scheduler()
            
            await interaction.response.send_message(
                f"Deleted habit: {name}",
                ephemeral=True
            )

    @app_commands.command(name="gentle-nudge", description="Get a gentle reminder of your tasks")
    async def gentle_nudge(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.bot.db_path) as db:
            cursor = await db.execute('''
                SELECT h.name, uh.current_streak, uh.last_check_in
                FROM habits h
                LEFT JOIN user_habits uh 
                    ON h.id = uh.habit_id 
                    AND uh.user_id = ?
            ''', (interaction.user.id,))
            
            habits = await cursor.fetchall()
            
            if not habits:
                await interaction.response.send_message(
                    "No habits to check! Use `/habit create` to get started.",
                    ephemeral=True
                )
                return
            
            message = ["Here's your gentle nudge! 🌸"]
            
            for name, streak, last_check_in in habits:
                if not last_check_in or (
                    datetime.now() - datetime.fromisoformat(last_check_in)
                ).days >= 1:
                    message.append(f"\n📝 Don't forget to check in for: {name}")
                    if streak and streak > 0:
                        message.append(f"   Current streak: {streak} day{'s' if streak != 1 else ''}")
            
            await interaction.response.send_message("\n".join(message), ephemeral=True)

    @app_commands.command(name="restock-add", description="Add an item to track for restocking")
    @app_commands.describe(
        item_name="Name of the item to track",
        days_until_refill="Number of days until refill is needed"
    )
    async def restock_add(
        self,
        interaction: discord.Interaction,
        item_name: str,
        days_until_refill: int
    ):
        if days_until_refill <= 0:
            await interaction.response.send_message(
                "Please provide a positive number of days!",
                ephemeral=True
            )
            return
        
        refill_date = datetime.now().date() + timedelta(days=days_until_refill)
        
        async with aiosqlite.connect(self.bot.db_path) as db:
            try:
                await db.execute(
                    '''INSERT INTO restock_items 
                       (user_id, item_name, refill_date, days_between_refills)
                       VALUES (?, ?, ?, ?)
                    ''',
                    (interaction.user.id, item_name, refill_date.isoformat(), days_until_refill)
                )
                await db.commit()
                
                await interaction.response.send_message(
                    f"I'll remind you to restock {item_name} in {days_until_refill} days! 📦",
                    ephemeral=True
                )
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(
                    f"You're already tracking an item called {item_name}! Use `/restock-done` to reset it.",
                    ephemeral=True
                )

    @app_commands.command(name="restock-done", description="Mark an item as restocked")
    @app_commands.describe(item_name="Name of the item that was restocked")
    async def restock_done(self, interaction: discord.Interaction, item_name: str):
        async with aiosqlite.connect(self.bot.db_path) as db:
            cursor = await db.execute(
                'SELECT days_between_refills FROM restock_items WHERE user_id = ? AND item_name = ?',
                (interaction.user.id, item_name)
            )
            row = await cursor.fetchone()
            
            if not row:
                await interaction.response.send_message(
                    f"I couldn't find an item called {item_name} in your restock list!",
                    ephemeral=True
                )
                return
            
            days_between_refills = row[0]
            next_refill = datetime.now().date() + timedelta(days=days_between_refills)
            
            await db.execute(
                'UPDATE restock_items SET refill_date = ? WHERE user_id = ? AND item_name = ?',
                (next_refill.isoformat(), interaction.user.id, item_name)
            )
            await db.commit()
            
            await interaction.response.send_message(
                f"Great job restocking {item_name}! I'll remind you again in {days_between_refills} days! 🎉",
                ephemeral=True
            )

async def setup(bot):
    bot.tree.add_command(HabitCommands(bot)) 