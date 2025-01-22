import os
import discord
from discord import app_commands
from datetime import datetime, timedelta
import random
import aiosqlite
import openai
from dotenv import load_dotenv
from openai import AsyncOpenAI

# Load environment variables
load_dotenv()
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')

# Configure OpenAI client for DeepSeek
client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1"  # DeepSeek's OpenAI-compatible endpoint
)

class HabitCommands(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="habit", description="Gentle habit tracking commands")
        self.bot = bot

    @app_commands.command(name="create", description="Create a new habit to track")
    @app_commands.describe(
        name="Name of the habit",
        reminder_time="Time for daily reminder (HH:MM in 24h format)",
        expiry_time="Time when the reminder expires (HH:MM in 24h format)",
        description="Optional description of the habit",
        participants="Users to ping for this habit (mention them)"
    )
    async def create_habit(
        self,
        interaction: discord.Interaction,
        name: str,
        reminder_time: str,
        expiry_time: str,
        description: str = None,
        participants: str = None
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
                # Insert the habit
                await db.execute(
                    '''INSERT INTO habits 
                       (name, reminder_time, expiry_time, description, created_at)
                       VALUES (?, ?, ?, ?, ?)''',
                    (name, reminder_time, expiry_time, description, datetime.now().isoformat())
                )
                await db.commit()
                
                # Get the habit ID
                cursor = await db.execute('SELECT id FROM habits WHERE name = ?', (name,))
                habit_id = (await cursor.fetchone())[0]
                
                # Add participants if specified
                if participants:
                    # Extract user IDs from mentions (format: <@123456789>)
                    user_ids = [int(uid.strip("<@>")) for uid in participants.split() if uid.startswith("<@") and uid.endswith(">")]
                    
                    # Add each participant
                    for user_id in user_ids:
                        await db.execute(
                            'INSERT INTO habit_participants (habit_id, user_id) VALUES (?, ?)',
                            (habit_id, user_id)
                        )
                    await db.commit()
                
                # Restart scheduler to include new habit
                await self.bot.setup_scheduler()
                
                # Create response message
                response = [
                    f"✨ Created new habit: {name}",
                    f"Daily reminder at: {reminder_time}",
                    f"Expires at: {expiry_time}"
                ]
                
                if participants:
                    participant_mentions = " ".join(f"<@{uid}>" for uid in user_ids)
                    response.append(f"Participants: {participant_mentions}")
                
                await interaction.response.send_message(
                    "\n".join(response),
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

    async def habit_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete handler for habit names"""
        async with aiosqlite.connect(self.bot.db_path) as db:
            # Get all habits that match the current input
            cursor = await db.execute(
                'SELECT name FROM habits WHERE name LIKE ? LIMIT 25',
                (f"%{current}%",)
            )
            habits = await cursor.fetchall()
            return [
                app_commands.Choice(name=habit[0], value=habit[0])
                for habit in habits
            ]

    @app_commands.command(name="delete", description="Delete a habit")
    @app_commands.describe(name="Name of the habit to delete")
    @app_commands.autocomplete(name=habit_name_autocomplete)
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
            await db.execute('DELETE FROM habit_participants WHERE habit_id = ?', (habit_id,))
            await db.execute('DELETE FROM habits WHERE id = ?', (habit_id,))
            await db.commit()
            
            # Restart scheduler to remove deleted habit
            await self.bot.setup_scheduler()
            
            await interaction.response.send_message(
                f"Deleted habit: {name}",
                ephemeral=True
            )

    @app_commands.command(name="edit", description="Edit an existing habit")
    @app_commands.describe(
        name="Name of the habit to edit",
        new_name="New name for the habit (optional)",
        reminder_time="New reminder time (HH:MM in 24h format, optional)",
        expiry_time="New expiry time (HH:MM in 24h format, optional)",
        description="New description (optional)",
        participants="Users to ping for this habit (mention them, optional)"
    )
    @app_commands.autocomplete(name=habit_name_autocomplete)
    async def edit_habit(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: str = None,
        reminder_time: str = None,
        expiry_time: str = None,
        description: str = None,
        participants: str = None
    ):
        # Validate time formats if provided
        if reminder_time:
            try:
                datetime.strptime(reminder_time, "%H:%M")
            except ValueError:
                await interaction.response.send_message(
                    "Please use HH:MM format for reminder time (e.g., 09:00, 14:30)",
                    ephemeral=True
                )
                return

        if expiry_time:
            try:
                datetime.strptime(expiry_time, "%H:%M")
            except ValueError:
                await interaction.response.send_message(
                    "Please use HH:MM format for expiry time (e.g., 09:00, 14:30)",
                    ephemeral=True
                )
                return

        async with aiosqlite.connect(self.bot.db_path) as db:
            # Check if habit exists
            cursor = await db.execute('SELECT id FROM habits WHERE name = ?', (name,))
            habit = await cursor.fetchone()
            
            if not habit:
                await interaction.response.send_message(
                    f"Could not find a habit named '{name}'",
                    ephemeral=True
                )
                return
            
            habit_id = habit[0]
            
            # Build update query dynamically based on provided fields
            update_fields = []
            params = []
            
            if new_name:
                update_fields.append("name = ?")
                params.append(new_name)
            if reminder_time:
                update_fields.append("reminder_time = ?")
                params.append(reminder_time)
            if expiry_time:
                update_fields.append("expiry_time = ?")
                params.append(expiry_time)
            if description:
                update_fields.append("description = ?")
                params.append(description)
            
            if update_fields:
                query = f"UPDATE habits SET {', '.join(update_fields)} WHERE id = ?"
                params.append(habit_id)
                try:
                    await db.execute(query, params)
                except aiosqlite.IntegrityError:
                    await interaction.response.send_message(
                        f"A habit with the name '{new_name}' already exists!",
                        ephemeral=True
                    )
                    return
            
            # Update participants if specified
            if participants:
                # Clear existing participants
                await db.execute('DELETE FROM habit_participants WHERE habit_id = ?', (habit_id,))
                
                # Add new participants
                user_ids = [int(uid.strip("<@>")) for uid in participants.split() if uid.startswith("<@") and uid.endswith(">")]
                for user_id in user_ids:
                    await db.execute(
                        'INSERT INTO habit_participants (habit_id, user_id) VALUES (?, ?)',
                        (habit_id, user_id)
                    )
            
            await db.commit()
            
            # Restart scheduler to apply changes
            await self.bot.setup_scheduler()
            
            # Create response message
            response = [f"✨ Updated habit: {new_name or name}"]
            if reminder_time:
                response.append(f"New reminder time: {reminder_time}")
            if expiry_time:
                response.append(f"New expiry time: {expiry_time}")
            if description:
                response.append(f"New description: {description}")
            if participants:
                participant_mentions = " ".join(f"<@{uid}>" for uid in user_ids)
                response.append(f"New participants: {participant_mentions}")
            
            await interaction.response.send_message(
                "\n".join(response),
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

    @app_commands.command(name="break-down", description="Break down a task into smaller, manageable steps")
    @app_commands.describe(
        task="The task you want to break down",
        complexity="Choose how detailed you want the breakdown to be",
        context="Any additional context about the task (optional)"
    )
    @app_commands.choices(complexity=[
        app_commands.Choice(name="Simple (3-5 steps)", value="simple"),
        app_commands.Choice(name="Medium (5-8 steps)", value="medium"),
        app_commands.Choice(name="Detailed (8-12 steps)", value="detailed")
    ])
    async def break_down_task(
        self,
        interaction: discord.Interaction,
        task: str,
        complexity: app_commands.Choice[str],
        context: str = None
    ):
        # Defer the response since API call might take time
        await interaction.response.defer(ephemeral=True)

        try:
            # Prepare the prompt based on complexity
            num_steps = {
                "simple": "3-5",
                "medium": "5-8",
                "detailed": "8-12"
            }[complexity.value]

            prompt = f"""Break down this task into {num_steps} small, manageable steps:
Task: {task}
{f'Context: {context}' if context else ''}

Please format each step like this:
1. [emoji] Step description (estimated time)

Make the steps:
- Specific and actionable
- ADHD-friendly (clear start/end points)
- Time-boxed (include estimated time)
- Encouraging and gentle in tone
- Each step should feel achievable in one sitting"""

            # Call DeepSeek API using new OpenAI format
            response = await client.chat.completions.create(
                model="deepseek-reasoner",  # Using chat model for more conversational responses
                messages=[
                    {"role": "system", "content": "You are a gentle, ADHD-friendly task breakdown assistant. You help break down tasks into manageable steps, always including emojis and time estimates. Your tone is warm and encouraging, and you make sure each step feels achievable."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7
            )

            # Create embed response
            embed = discord.Embed(
                title=f"✨ Task Breakdown: {task}",
                description="Here's your gentle task breakdown:",
                color=discord.Color.blue()
            )

            # Add the steps to the embed
            steps = response.choices[0].message.content.strip().split('\n')
            for step in steps:
                if step.strip():  # Skip empty lines
                    embed.add_field(
                        name="Step",
                        value=step,
                        inline=False
                    )

            # Add footer with context if provided
            if context:
                embed.set_footer(text=f"Context: {context}")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(
                "I had trouble breaking down that task. Please try again or make the task more specific.",
                ephemeral=True
            )
            print(f"Error in break_down_task: {str(e)}")  # Log the error

    @app_commands.command(name="help", description="Show all available commands and how to use them")
    async def show_help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🌟 Gentle Habits Bot - Help Guide",
            description="Here's how I can help you build gentle habits!",
            color=discord.Color.blue()
        )

        # Habit Management
        embed.add_field(
            name="📝 Habit Management",
            value="""
• `/habit create` - Create a new habit to track
  - Set reminder time and expiry time (HH:MM format)
  - Add optional description and participants
• `/habit list` - View all your habits and streaks
• `/habit edit` - Modify an existing habit
• `/habit delete` - Remove a habit
""",
            inline=False
        )

        # Task Breakdown
        embed.add_field(
            name="✨ Task Breakdown",
            value="""
• `/habit break-down` - Break down a task into manageable steps
  - Choose complexity: Simple (3-5 steps), Medium (5-8), or Detailed (8-12)
  - Add optional context for better breakdown
""",
            inline=False
        )

        # Restock System
        embed.add_field(
            name="📦 Restock Tracking",
            value="""
• `/habit restock-add` - Track an item for restocking
  - Set number of days until refill needed
• `/habit restock-done` - Mark an item as restocked
""",
            inline=False
        )

        # Daily Support
        embed.add_field(
            name="🌸 Daily Support",
            value="""
• `/habit gentle-nudge` - Get a friendly reminder of your tasks
  - Shows current streaks and pending check-ins
""",
            inline=False
        )

        # Tips footer
        embed.set_footer(text="💡 Most responses are ephemeral (only visible to you) for privacy!")

        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    bot.tree.add_command(HabitCommands(bot)) 