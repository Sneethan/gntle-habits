import os
import discord
from discord import app_commands
from datetime import datetime, timedelta
import random
import aiosqlite
import openai
from dotenv import load_dotenv
from openai import AsyncOpenAI
import aiohttp
import json

# Load environment variables
load_dotenv()
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')

# Configure OpenAI client for DeepSeek
client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1"  # DeepSeek's OpenAI-compatible endpoint
)

async def check_deepseek_status():
    """Check if DeepSeek API is experiencing a major outage."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://status.deepseek.com/api/v2/components.json') as response:
                if response.status == 200:
                    data = await response.json()
                    api_component = next(
                        (comp for comp in data['components'] 
                         if comp['name'] == 'API 服务 (API Service)'),
                        None
                    )
                    if api_component and api_component['status'] == 'major_outage':
                        return False, "DeepSeek API is currently experiencing a major outage. Please try again later."
                return True, None
    except Exception as e:
        return False, f"Unable to check DeepSeek API status: {str(e)}"

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
            # Check DeepSeek API status first
            api_available, error_message = await check_deepseek_status()
            if not api_available:
                await interaction.followup.send(error_message, ephemeral=True)
                return

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

    @app_commands.command(name="organise", description="Get an ADHD-friendly organization of multiple tasks with time estimates and gentle steps")
    @app_commands.describe(
        tasks="List all your tasks, separated by newlines or commas",
        energy_level="Your current energy level",
        time_available="How much time you have available (e.g. '2 hours', '30 minutes')",
        priority_type="How you want to prioritize tasks"
    )
    @app_commands.choices(energy_level=[
        app_commands.Choice(name="High Energy ⚡", value="high"),
        app_commands.Choice(name="Medium Energy 💫", value="medium"),
        app_commands.Choice(name="Low Energy 🌸", value="low"),
        app_commands.Choice(name="Very Low Energy 🌙", value="very_low")
    ])
    @app_commands.choices(priority_type=[
        app_commands.Choice(name="Urgency First 🚨", value="urgency"),
        app_commands.Choice(name="Quick Wins First ✨", value="quick_wins"),
        app_commands.Choice(name="Energy Based 🔋", value="energy"),
        app_commands.Choice(name="Importance First 🎯", value="importance")
    ])
    async def organise_tasks(
        self,
        interaction: discord.Interaction,
        tasks: str,
        energy_level: app_commands.Choice[str],
        time_available: str = None,
        priority_type: app_commands.Choice[str] = None
    ):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Check DeepSeek API status first
            api_available, error_message = await check_deepseek_status()
            if not api_available:
                await interaction.followup.send(error_message, ephemeral=True)
                return

            # Prepare the prompt for DeepSeek
            system_prompt = """You are a supportive ADHD coach who helps organize tasks in a manageable way.
            Your strengths are:
            1. Breaking down overwhelming task lists into achievable chunks
            2. Matching tasks to current energy levels
            3. Providing realistic time estimates
            4. Suggesting task pairings and body doubles
            5. Identifying quick wins and momentum builders
            6. Adding encouraging notes without being overly positive
            
            Format your response in clear sections using Discord markdown:
            - 📋 Task Overview
            - ⚡ Energy-Matched Tasks
            - ⏰ Time Estimates
            - 🎯 First Steps
            - 💫 Quick Wins
            - 🌟 Helpful Tips"""

            user_prompt = f"""Here are my tasks to organize:
{tasks}

My current energy level is: {energy_level.name}
{f'I have {time_available} available' if time_available else ''}
{f'Please prioritize by {priority_type.name}' if priority_type else ''}

Please help me organize these tasks in an ADHD-friendly way that:
1. Matches my current energy level
2. Includes very specific first steps
3. Identifies any quick wins
4. Suggests task pairings or body doubling opportunities
5. Provides realistic time estimates
6. Adds encouraging but realistic notes"""

            # Call DeepSeek Chat
            response = await client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=1500
            )

            breakdown = response.choices[0].message.content

            # Create embed response
            embed = discord.Embed(
                title="✨ ADHD-Friendly Task Organization",
                description=f"Energy Level: {energy_level.name}\n{f'Time Available: {time_available}' if time_available else ''}",
                color=discord.Color.purple()
            )

            # Split the response into sections and add them to the embed
            sections = breakdown.split('\n\n')
            for section in sections:
                if section.strip():
                    # Extract title and content
                    parts = section.split('\n', 1)
                    if len(parts) > 1:
                        title = parts[0].strip('# -')
                        content = parts[1].strip()
                        embed.add_field(name=title, value=content, inline=False)

            # Add footer with gentle reminder
            embed.set_footer(text="Remember: You don't have to do everything at once. Start small and celebrate your progress! 💝")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(
                "❌ Sorry, I had trouble organizing your tasks. Please try again or break your list into smaller chunks.",
                ephemeral=True
            )
            raise e

    @app_commands.command(name="motivate", description="Get encouraging reasons and motivation for a task")
    @app_commands.describe(
        task="The task you want motivation for",
        perspective="Choose what kind of motivation you need",
        context="Any additional context about the task (optional)"
    )
    @app_commands.choices(perspective=[
        app_commands.Choice(name="Future Benefits (what you'll gain)", value="benefits"),
        app_commands.Choice(name="Present Moment (making it enjoyable now)", value="present"),
        app_commands.Choice(name="Past Success (remember similar wins)", value="past"),
        app_commands.Choice(name="Gentle Support (kind encouragement)", value="gentle")
    ])
    async def motivate_task(
        self,
        interaction: discord.Interaction,
        task: str,
        perspective: app_commands.Choice[str],
        context: str = None
    ):
        # Defer the response since API call might take time
        await interaction.response.defer(ephemeral=True)

        try:
            # Check DeepSeek API status first
            api_available, error_message = await check_deepseek_status()
            if not api_available:
                await interaction.followup.send(error_message, ephemeral=True)
                return

            # Prepare the prompt based on perspective
            prompts = {
                "benefits": "Focus on future benefits and positive outcomes. What will they gain? How will this improve their life?",
                "present": "Focus on making the task enjoyable or satisfying in the present moment. How can we reframe it positively?",
                "past": "Focus on past successes and similar achievements. What strengths have they shown before?",
                "gentle": "Provide gentle, understanding encouragement. Acknowledge difficulties while highlighting capabilities."
            }

            base_prompt = f"""Help find positive motivation for this task:
Task: {task}
{f'Context: {context}' if context else ''}

Please provide:
1. 🎯 Main Benefit/Reason
2. 💫 Three Positive Points
3. 🌟 One Gentle Reminder
4. ✨ Small First Step

Focus: {prompts[perspective.value]}

Make the response:
- Encouraging and gentle in tone
- ADHD-friendly (clear and engaging)
- Specific to the task
- Empowering without pressure
- DO NOT use ### in your headings."""

            # Call DeepSeek API
            response = await client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a compassionate motivation coach who helps people find genuine, intrinsic motivation for tasks. You understand ADHD challenges and provide gentle, specific encouragement without toxic positivity. Your responses are always kind, realistic, and focused on growth."},
                    {"role": "user", "content": base_prompt}
                ],
                temperature=0.7
            )

            # Create embed response
            embed = discord.Embed(
                title=f"✨ Finding Joy in: {task}",
                description="Here's some gentle encouragement:",
                color=discord.Color.purple()
            )

            # Add the motivation points to the embed
            sections = response.choices[0].message.content.strip().split('\n')
            current_field = ""
            current_content = []
            
            for line in sections:
                if line.strip():
                    if any(marker in line for marker in ['🎯', '💫', '🌟', '✨']):
                        # If we have a previous field ready, add it
                        if current_field and current_content:
                            embed.add_field(
                                name=current_field,
                                value='\n'.join(current_content),
                                inline=False
                            )
                            current_content = []
                        current_field = line
                    else:
                        current_content.append(line)
            
            # Add the last field
            if current_field and current_content:
                embed.add_field(
                    name=current_field,
                    value='\n'.join(current_content),
                    inline=False
                )

            # Add footer with context if provided
            if context:
                embed.set_footer(text=f"Context: {context}")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(
                "I had trouble finding motivation for that task. Please try again with a different description.",
                ephemeral=True
            )
            print(f"Error in motivate_task: {str(e)}")  # Log the error

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

        # Task Support
        embed.add_field(
            name="✨ Task Support",
            value="""
• `/habit break-down` - Break down a task into manageable steps
  - Choose complexity: Simple (3-5 steps), Medium (5-8), or Detailed (8-12)
• `/habit motivate` - Get encouraging reasons for doing a task
  - Choose perspective: Future Benefits, Present Moment, Past Success, or Gentle Support
• `/habit timer` - Set a gentle Pomodoro-style timer
  - Flexible durations and break times
  - Encouraging messages and break suggestions
""",
            inline=False
        )

        # Celebration and Progress
        embed.add_field(
            name="🎉 Celebration & Progress",
            value="""
• `/habit celebrate` - Record and celebrate achievements
  - Track wins of any size
  - Different categories for various types of success
• `/habit celebration-history` - View your past celebrations
  - Filter by category and timeframe
  - See your progress over time
""",
            inline=False
        )

        # Environment and Energy
        embed.add_field(
            name="🌸 Environment & Energy",
            value="""
• `/habit energy-check` - Match tasks to your current energy
  - Get suggestions based on energy and focus levels
  - Adapt your habits to how you're feeling
• `/habit setup-space` - Create an optimal environment
  - Customized for different activities
  - Consider sensory needs and preferences
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
            name="💫 Daily Support",
            value="""
• `/habit gentle-nudge` - Get a friendly reminder of your tasks
  - Shows current streaks and pending check-ins
  - Gentle encouragement without pressure
""",
            inline=False
        )

        # Tips footer
        embed.set_footer(text="💡 Most responses are ephemeral (only visible to you) for privacy!")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="timer", description="Set a gentle Pomodoro-style timer with flexible durations")
    @app_commands.describe(
        duration="How long to focus (in minutes)",
        activity="What you'll be working on",
        break_duration="Length of break after focus time (in minutes)",
        reminder_type="How you want to be reminded"
    )
    @app_commands.choices(duration=[
        app_commands.Choice(name="Quick Focus (15 min)", value=15),
        app_commands.Choice(name="Standard Focus (25 min)", value=25),
        app_commands.Choice(name="Extended Focus (45 min)", value=45),
        app_commands.Choice(name="Deep Focus (60 min)", value=60)
    ])
    @app_commands.choices(break_duration=[
        app_commands.Choice(name="Short Break (5 min)", value=5),
        app_commands.Choice(name="Regular Break (10 min)", value=10),
        app_commands.Choice(name="Long Break (15 min)", value=15)
    ])
    @app_commands.choices(reminder_type=[
        app_commands.Choice(name="Gentle (just text)", value="gentle"),
        app_commands.Choice(name="Standard (with emoji)", value="standard"),
        app_commands.Choice(name="Encouraging (with message)", value="encouraging")
    ])
    async def timer(
        self,
        interaction: discord.Interaction,
        duration: app_commands.Choice[int],
        activity: str,
        break_duration: app_commands.Choice[int],
        reminder_type: app_commands.Choice[str]
    ):
        await interaction.response.defer(ephemeral=True)
        
        # Create initial embed
        embed = discord.Embed(
            title="🎯 Focus Timer Started",
            description=f"Let's work on: {activity}",
            color=discord.Color.green()
        )
        
        embed.add_field(
            name="⏱️ Duration",
            value=f"{duration.value} minutes of focus time\n{break_duration.value} minutes break after",
            inline=False
        )
        
        # Add encouraging message based on duration
        messages = {
            15: "Perfect for quick tasks! You've got this! 💫",
            25: "Classic Pomodoro time! Ready to focus! ✨",
            45: "Great for deeper work! You can do this! 🌟",
            60: "Deep focus mode activated! Remember to stay hydrated! 🌊"
        }
        embed.add_field(
            name="💝 Remember",
            value=messages[duration.value],
            inline=False
        )
        
        # Send initial message
        timer_message = await interaction.followup.send(embed=embed, ephemeral=True)
        
        # Schedule focus time end notification
        self.bot.scheduler.add_job(
            self.timer_focus_end,
            'date',
            run_date=datetime.now() + timedelta(minutes=duration.value),
            args=[interaction.user.id, timer_message.id, activity, break_duration.value, reminder_type.value]
        )
        
    async def timer_focus_end(self, user_id: int, message_id: int, activity: str, break_duration: int, reminder_type: str):
        """Handle focus timer completion and break start."""
        user = self.bot.get_user(user_id)
        if not user:
            return
            
        # Create break start embed
        embed = discord.Embed(
            title="🌟 Focus Time Complete!",
            description=f"Great work on: {activity}",
            color=discord.Color.purple()
        )
        
        # Add message based on reminder type
        messages = {
            "gentle": f"Time for a {break_duration} minute break.",
            "standard": f"🎉 Well done! Enjoy your {break_duration} minute break! ✨",
            "encouraging": f"Amazing job staying focused! You've earned a {break_duration} minute break!\n💝 Remember: Progress isn't always linear, and you're doing great!"
        }
        embed.add_field(
            name="💫 Next Step",
            value=messages[reminder_type],
            inline=False
        )
        
        # Add break suggestions
        break_suggestions = [
            "🧘‍♀️ Do some light stretching",
            "💧 Drink some water",
            "👀 Look at something 20 feet away for 20 seconds",
            "🚶‍♂️ Take a short walk",
            "🌱 Check on your plants",
            "✨ Tidy one small thing"
        ]
        embed.add_field(
            name="Break Ideas",
            value="\n".join(random.sample(break_suggestions, 3)),
            inline=False
        )
        
        try:
            # Try to DM the user
            await user.send(embed=embed)
            
            # Schedule break end notification
            self.bot.scheduler.add_job(
                self.timer_break_end,
                'date',
                run_date=datetime.now() + timedelta(minutes=break_duration),
                args=[user_id, activity, reminder_type]
            )
        except discord.Forbidden:
            # If DM fails, log it
            print(f"Could not send timer notification to user {user_id}")
            
    async def timer_break_end(self, user_id: int, activity: str, reminder_type: str):
        """Handle break timer completion."""
        user = self.bot.get_user(user_id)
        if not user:
            return
            
        # Create break end embed
        embed = discord.Embed(
            title="🌸 Break Time Complete",
            description="Ready to return to your task?",
            color=discord.Color.blue()
        )
        
        # Add message based on reminder type
        messages = {
            "gentle": f"You can return to {activity} now.",
            "standard": f"🎯 Time to continue with {activity}! You've got this! ✨",
            "encouraging": f"Refreshed and ready? Let's continue with {activity}!\n💫 Remember: Every little bit of progress counts!"
        }
        embed.add_field(
            name="Next Steps",
            value=messages[reminder_type],
            inline=False
        )
        
        try:
            await user.send(embed=embed)
        except discord.Forbidden:
            print(f"Could not send break end notification to user {user_id}")

    @app_commands.command(name="celebrate", description="Record and celebrate your achievements, big or small!")
    @app_commands.describe(
        achievement="What you accomplished",
        category="Type of achievement",
        feeling="How it made you feel (optional)",
        difficulty="How challenging was it?"
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="Task Completion 📝", value="task"),
        app_commands.Choice(name="Self Care 🌸", value="self_care"),
        app_commands.Choice(name="Social Success 🤝", value="social"),
        app_commands.Choice(name="Creative Win 🎨", value="creative"),
        app_commands.Choice(name="Routine Victory ⭐", value="routine")
    ])
    @app_commands.choices(difficulty=[
        app_commands.Choice(name="Small Win (but still counts!)", value="small"),
        app_commands.Choice(name="Medium Challenge", value="medium"),
        app_commands.Choice(name="Big Achievement", value="big")
    ])
    async def celebrate(
        self,
        interaction: discord.Interaction,
        achievement: str,
        category: app_commands.Choice[str],
        difficulty: app_commands.Choice[str],
        feeling: str = None
    ):
        # Create celebrations table if it doesn't exist
        async with aiosqlite.connect(self.bot.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS celebrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    achievement TEXT NOT NULL,
                    category TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    feeling TEXT,
                    celebrated_at TEXT NOT NULL
                )
            ''')
            
            # Record the celebration
            await db.execute('''
                INSERT INTO celebrations 
                (user_id, achievement, category, difficulty, feeling, celebrated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                interaction.user.id,
                achievement,
                category.value,
                difficulty.value,
                feeling,
                datetime.now().isoformat()
            ))
            await db.commit()
            
            # Get celebration count for this category
            cursor = await db.execute(
                'SELECT COUNT(*) FROM celebrations WHERE user_id = ? AND category = ?',
                (interaction.user.id, category.value)
            )
            category_count = (await cursor.fetchone())[0]
            
            # Get total celebration count
            cursor = await db.execute(
                'SELECT COUNT(*) FROM celebrations WHERE user_id = ?',
                (interaction.user.id,)
            )
            total_count = (await cursor.fetchone())[0]
        
        # Create response embed
        embed = discord.Embed(
            title="🎉 Time to Celebrate!",
            description=f"Congratulations on your {difficulty.value} win!",
            color=discord.Color.gold()
        )
        
        # Add achievement details
        embed.add_field(
            name=f"{category.name}",
            value=achievement,
            inline=False
        )
        
        if feeling:
            embed.add_field(
                name="💭 Feeling",
                value=feeling,
                inline=False
            )
        
        # Add encouraging message based on difficulty
        messages = {
            "small": "Remember: Small wins add up to big progress! 💫",
            "medium": "You tackled this challenge and succeeded! 🌟",
            "big": "This is a major achievement! You should be really proud! ✨"
        }
        embed.add_field(
            name="💝 Remember",
            value=messages[difficulty.value],
            inline=False
        )
        
        # Add progress stats
        embed.add_field(
            name="📊 Your Progress",
            value=f"This is your {category_count}th {category.name} celebration!\nTotal celebrations: {total_count}",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    @app_commands.command(name="celebration-history", description="View your past celebrations and achievements")
    @app_commands.describe(
        category="Filter by category (optional)",
        timeframe="How far back to look"
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="All Celebrations ✨", value="all"),
        app_commands.Choice(name="Task Completion 📝", value="task"),
        app_commands.Choice(name="Self Care 🌸", value="self_care"),
        app_commands.Choice(name="Social Success 🤝", value="social"),
        app_commands.Choice(name="Creative Win 🎨", value="creative"),
        app_commands.Choice(name="Routine Victory ⭐", value="routine")
    ])
    @app_commands.choices(timeframe=[
        app_commands.Choice(name="Past Week", value="week"),
        app_commands.Choice(name="Past Month", value="month"),
        app_commands.Choice(name="All Time", value="all")
    ])
    async def celebration_history(
        self,
        interaction: discord.Interaction,
        timeframe: app_commands.Choice[str],
        category: app_commands.Choice[str] = None
    ):
        await interaction.response.defer(ephemeral=True)
        
        # Calculate date range
        now = datetime.now()
        if timeframe.value == "week":
            start_date = (now - timedelta(days=7)).isoformat()
        elif timeframe.value == "month":
            start_date = (now - timedelta(days=30)).isoformat()
        else:
            start_date = "1970-01-01"  # All time
            
        # Build query based on category filter
        query = '''
            SELECT achievement, category, difficulty, feeling, celebrated_at
            FROM celebrations 
            WHERE user_id = ? AND celebrated_at >= ?
        '''
        params = [interaction.user.id, start_date]
        
        if category and category.value != "all":
            query += ' AND category = ?'
            params.append(category.value)
            
        query += ' ORDER BY celebrated_at DESC LIMIT 10'
        
        async with aiosqlite.connect(self.bot.db_path) as db:
            cursor = await db.execute(query, params)
            celebrations = await cursor.fetchall()
            
            if not celebrations:
                await interaction.followup.send(
                    "No celebrations found for this timeframe. Time to create some new wins! ✨",
                    ephemeral=True
                )
                return
            
            # Create embed
            embed = discord.Embed(
                title="🌟 Your Celebration Journey",
                description=f"Here are your recent wins ({timeframe.name}):",
                color=discord.Color.purple()
            )
            
            # Group celebrations by category
            categories = {}
            for achievement, cat, diff, feeling, date in celebrations:
                if cat not in categories:
                    categories[cat] = []
                celebrated_at = datetime.fromisoformat(date)
                categories[cat].append({
                    'achievement': achievement,
                    'difficulty': diff,
                    'date': celebrated_at.strftime("%Y-%m-%d")
                })
            
            # Add fields for each category
            category_names = {
                "task": "Task Completion 📝",
                "self_care": "Self Care 🌸",
                "social": "Social Success 🤝",
                "creative": "Creative Win 🎨",
                "routine": "Routine Victory ⭐"
            }
            
            for cat, items in categories.items():
                value = "\n".join([
                    f"• {item['achievement']} ({item['difficulty']}) - {item['date']}"
                    for item in items
                ])
                embed.add_field(
                    name=category_names.get(cat, cat),
                    value=value,
                    inline=False
                )
            
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="energy-check", description="Match tasks to your current energy level")
    @app_commands.describe(
        energy_level="Your current energy level",
        focus_level="Your current ability to focus",
        environment="Your current environment",
        duration="How long you can work for"
    )
    @app_commands.choices(energy_level=[
        app_commands.Choice(name="High Energy ⚡", value="high"),
        app_commands.Choice(name="Medium Energy 💫", value="medium"),
        app_commands.Choice(name="Low Energy 🌸", value="low"),
        app_commands.Choice(name="Very Low Energy 🌙", value="very_low")
    ])
    @app_commands.choices(focus_level=[
        app_commands.Choice(name="Sharp Focus 🎯", value="sharp"),
        app_commands.Choice(name="Moderate Focus ✨", value="moderate"),
        app_commands.Choice(name="Scattered Focus 🍃", value="scattered"),
        app_commands.Choice(name="No Focus 💭", value="none")
    ])
    @app_commands.choices(environment=[
        app_commands.Choice(name="Quiet Space 🏡", value="quiet"),
        app_commands.Choice(name="Some Background Noise 🎶", value="some_noise"),
        app_commands.Choice(name="Busy Environment 🏙️", value="busy"),
        app_commands.Choice(name="On the Move 🚶", value="mobile")
    ])
    @app_commands.choices(duration=[
        app_commands.Choice(name="Quick Task (5-15 min)", value="quick"),
        app_commands.Choice(name="Short Session (15-30 min)", value="short"),
        app_commands.Choice(name="Medium Session (30-60 min)", value="medium"),
        app_commands.Choice(name="Long Session (60+ min)", value="long")
    ])
    async def energy_check(
        self,
        interaction: discord.Interaction,
        energy_level: app_commands.Choice[str],
        focus_level: app_commands.Choice[str],
        environment: app_commands.Choice[str],
        duration: app_commands.Choice[str]
    ):
        await interaction.response.defer(ephemeral=True)
        
        # Get user's habits and tasks
        async with aiosqlite.connect(self.bot.db_path) as db:
            cursor = await db.execute('''
                SELECT h.name, h.description
                FROM habits h
                JOIN habit_participants hp ON h.id = hp.habit_id
                WHERE hp.user_id = ?
            ''', (interaction.user.id,))
            habits = await cursor.fetchall()
        
        # Create task suggestions based on energy levels
        task_suggestions = {
            # High energy suggestions
            ("high", "sharp"): [
                "🎯 Perfect for challenging tasks requiring full attention",
                "📚 Tackle complex learning or problem-solving",
                "✍️ Creative work or brainstorming",
                "🗂️ Organization and planning"
            ],
            ("high", "moderate"): [
                "📝 Writing or content creation",
                "🤝 Social interactions or meetings",
                "🎨 Creative projects",
                "📊 Data analysis or research"
            ],
            # Medium energy suggestions
            ("medium", "sharp"): [
                "📋 Administrative tasks",
                "📧 Email management",
                "📱 Digital organization",
                "🗄️ File sorting and cleanup"
            ],
            ("medium", "moderate"): [
                "📚 Light reading",
                "📝 Note-taking",
                "🎧 Audio content consumption",
                "🗂️ Simple organizing tasks"
            ],
            # Low energy suggestions
            ("low", "scattered"): [
                "🧹 Light cleaning or tidying",
                "📱 Simple digital tasks",
                "📦 Basic sorting",
                "🌱 Plant care or simple self-care"
            ],
            ("low", "none"): [
                "💧 Hydration and snack prep",
                "🧘‍♀️ Gentle movement",
                "🌸 Basic self-care",
                "✨ Small environment improvements"
            ],
            # Very low energy suggestions
            ("very_low", "scattered"): [
                "🌸 Minimal self-care tasks",
                "💭 Gentle planning",
                "🎵 Music listening",
                "🌱 Very simple environment care"
            ],
            ("very_low", "none"): [
                "💝 Rest and recharge",
                "🌙 Gentle self-care",
                "🧘‍♀️ Deep breathing",
                "💭 Mindful moments"
            ]
        }
        
        # Get appropriate suggestions
        suggestions = task_suggestions.get(
            (energy_level.value, focus_level.value),
            ["✨ Gentle self-care activities", "🌸 Simple tasks", "💫 Basic maintenance"]
        )
        
        # Create embed response
        embed = discord.Embed(
            title="🌟 Energy Level Check",
            description="Let's match tasks to your current state!",
            color=discord.Color.blue()
        )
        
        # Current state field
        embed.add_field(
            name="💫 Current State",
            value=f"Energy: {energy_level.name}\nFocus: {focus_level.name}\nEnvironment: {environment.name}\nDuration: {duration.name}",
            inline=False
        )
        
        # Task suggestions field
        embed.add_field(
            name="✨ Suggested Activities",
            value="\n".join(suggestions),
            inline=False
        )
        
        # Add habit-specific suggestions if available
        if habits:
            habit_suggestions = []
            for name, description in habits:
                if energy_level.value in ["high", "medium"]:
                    habit_suggestions.append(f"• {name} - Good time for habit work!")
                elif energy_level.value == "low":
                    habit_suggestions.append(f"• {name} - Consider a smaller version of this habit")
                else:
                    habit_suggestions.append(f"• {name} - Save this for when energy is higher")
            
            embed.add_field(
                name="🌱 Your Habits",
                value="\n".join(habit_suggestions[:3]) + "\n*(Showing top 3 habits)*",
                inline=False
            )
        
        # Environment tips based on current setting
        environment_tips = {
            "quiet": "Perfect for focused work! Consider using this time for tasks needing concentration.",
            "some_noise": "Good balance! Background noise can help with focus for some tasks.",
            "busy": "Consider noise-cancelling headphones or finding a quieter spot if needed.",
            "mobile": "Focus on tasks that can be done while moving or with minimal setup."
        }
        
        embed.add_field(
            name="🏡 Environment Note",
            value=environment_tips[environment.value],
            inline=False
        )
        
        # Duration-based recommendation
        duration_tips = {
            "quick": "Perfect for small tasks! Break larger tasks into 5-15 minute chunks.",
            "short": "Good for focused sprints! Consider using a timer for structure.",
            "medium": "Great for deeper work! Remember to take a break halfway through.",
            "long": "Excellent for big tasks! Plan 5-minute breaks every 25-30 minutes."
        }
        
        embed.add_field(
            name="⏱️ Time Management",
            value=duration_tips[duration.value],
            inline=False
        )
        
        # Add a gentle reminder
        embed.set_footer(text="Remember: Your energy levels are valid, and it's okay to match your tasks to how you're feeling right now! 💝")
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="setup-space", description="Get a checklist for creating an optimal environment for your task")
    @app_commands.describe(
        activity_type="What kind of activity are you planning",
        sensory_needs="Your current sensory preferences",
        space_type="Where you'll be working",
        duration="How long you'll be working"
    )
    @app_commands.choices(activity_type=[
        app_commands.Choice(name="Focus Work 🎯", value="focus"),
        app_commands.Choice(name="Creative Work 🎨", value="creative"),
        app_commands.Choice(name="Physical Tasks 💪", value="physical"),
        app_commands.Choice(name="Rest/Recharge 🌙", value="rest"),
        app_commands.Choice(name="Social/Meeting 🤝", value="social")
    ])
    @app_commands.choices(sensory_needs=[
        app_commands.Choice(name="Need Quiet 🤫", value="quiet"),
        app_commands.Choice(name="Like Background Noise 🎶", value="noise"),
        app_commands.Choice(name="Need Movement 🚶", value="movement"),
        app_commands.Choice(name="Prefer Low Stimulation 🌸", value="low_stim"),
        app_commands.Choice(name="Need High Stimulation ⚡", value="high_stim")
    ])
    @app_commands.choices(space_type=[
        app_commands.Choice(name="Desk/Table 🪑", value="desk"),
        app_commands.Choice(name="Cozy Space 🛋️", value="cozy"),
        app_commands.Choice(name="Floor Space 🧘‍♀️", value="floor"),
        app_commands.Choice(name="Outside 🌳", value="outside"),
        app_commands.Choice(name="Mobile/Moving 🚶", value="mobile")
    ])
    @app_commands.choices(duration=[
        app_commands.Choice(name="Quick Session (15-30 min)", value="quick"),
        app_commands.Choice(name="Medium Session (30-60 min)", value="medium"),
        app_commands.Choice(name="Long Session (60+ min)", value="long")
    ])
    async def setup_space(
        self,
        interaction: discord.Interaction,
        activity_type: app_commands.Choice[str],
        sensory_needs: app_commands.Choice[str],
        space_type: app_commands.Choice[str],
        duration: app_commands.Choice[str]
    ):
        await interaction.response.defer(ephemeral=True)
        
        # Basic setup checklist that applies to all situations
        basic_checklist = [
            "🧊 Water or preferred drink nearby",
            "🚽 Quick bathroom break before starting",
            "📱 Phone on silent/Do Not Disturb",
            "🌡️ Check temperature comfort"
        ]
        
        # Activity-specific setup suggestions
        activity_setups = {
            "focus": [
                "📱 Close unnecessary browser tabs/apps",
                "📝 Have notes/materials ready",
                "⏰ Set a timer for focused work",
                "🎧 Prepare focus sounds/music if needed"
            ],
            "creative": [
                "🎨 Clear space for materials",
                "✨ Gather all needed supplies",
                "💡 Set up good lighting",
                "🎵 Prepare inspiring playlist"
            ],
            "physical": [
                "👕 Comfortable clothes ready",
                "🧘‍♀️ Clear enough space to move",
                "💧 Water bottle filled",
                "🧺 Towel or cleanup supplies nearby"
            ],
            "rest": [
                "🛋️ Prepare comfortable seating/lying space",
                "🌸 Dim lights or adjust blinds",
                "🧸 Comfort items within reach",
                "🎵 Calming sounds/white noise ready"
            ],
            "social": [
                "🎥 Test camera/mic if needed",
                "📝 Meeting materials ready",
                "👕 Check appearance if on video",
                "🎧 Headphones charged/ready"
            ]
        }
        
        # Sensory accommodation suggestions
        sensory_setups = {
            "quiet": [
                "🎧 Noise-cancelling headphones ready",
                "🚪 Close doors/windows to reduce noise",
                "📱 All notifications muted",
                "💭 Consider white noise if helpful"
            ],
            "noise": [
                "🎵 Prepare background playlist",
                "🎧 Headphones ready",
                "🎶 Test audio levels",
                "📝 Have backup noise options ready"
            ],
            "movement": [
                "🪑 Fidget tools nearby",
                "💺 Consider standing desk setup",
                "🚶 Clear path for pacing",
                "⏰ Set movement break reminders"
            ],
            "low_stim": [
                "💡 Adjust lighting to be softer",
                "🌸 Remove visual clutter",
                "🧸 Comfort items nearby",
                "🎨 Consider using neutral colors"
            ],
            "high_stim": [
                "✨ Add engaging visual elements",
                "🎵 Upbeat music ready",
                "💡 Bright, energizing lighting",
                "🎨 Colorful items or decorations"
            ]
        }
        
        # Space-specific setup tips
        space_setups = {
            "desk": [
                "🪑 Adjust chair height",
                "💻 Position screen at eye level",
                "📚 Clear unnecessary items",
                "💡 Check lighting position"
            ],
            "cozy": [
                "🛋️ Arrange pillows/supports",
                "🌸 Add comfort items",
                "💡 Adjust ambient lighting",
                "🧸 Keep essentials within reach"
            ],
            "floor": [
                "🧘‍♀️ Prepare cushions/mat",
                "📏 Ensure enough space",
                "💡 Check lighting angles",
                "🧺 Keep area clear and clean"
            ],
            "outside": [
                "☀️ Check weather conditions",
                "🧢 Sun protection if needed",
                "🦟 Bug spray if necessary",
                "🎒 Pack mobile essentials"
            ],
            "mobile": [
                "🎒 Pack light but complete",
                "🔋 Check device charges",
                "📱 Download needed materials",
                "💧 Portable water/snacks ready"
            ]
        }
        
        # Duration-specific reminders
        duration_setups = {
            "quick": [
                "⏰ Set a clear timer",
                "📝 Have a focused task list",
                "🎯 Remove major distractions",
                "💫 Keep setup simple"
            ],
            "medium": [
                "⏰ Plan one short break",
                "💧 Prepare water/snack",
                "🪑 Check comfort for longer sit",
                "📱 Set up Do Not Disturb"
            ],
            "long": [
                "⏰ Schedule regular breaks",
                "🥪 Plan for meals/snacks",
                "💧 Multiple water sources",
                "🧘‍♀️ Include movement options"
            ]
        }
        
        # Create embed response
        embed = discord.Embed(
            title="🌟 Space Setup Guide",
            description=f"Let's create the perfect environment for your {activity_type.name}!",
            color=discord.Color.green()
        )
        
        # Add basic checklist
        embed.add_field(
            name="✨ Basic Setup",
            value="\n".join(f"• {item}" for item in basic_checklist),
            inline=False
        )
        
        # Add activity-specific setup
        embed.add_field(
            name=f"{activity_type.name} Setup",
            value="\n".join(f"• {item}" for item in activity_setups[activity_type.value]),
            inline=False
        )
        
        # Add sensory accommodations
        embed.add_field(
            name="🌸 Sensory Setup",
            value="\n".join(f"• {item}" for item in sensory_setups[sensory_needs.value]),
            inline=False
        )
        
        # Add space-specific setup
        embed.add_field(
            name="🏡 Space Setup",
            value="\n".join(f"• {item}" for item in space_setups[space_type.value]),
            inline=False
        )
        
        # Add duration-specific reminders
        embed.add_field(
            name="⏱️ Time Setup",
            value="\n".join(f"• {item}" for item in duration_setups[duration.value]),
            inline=False
        )
        
        # Add footer with gentle reminder
        embed.set_footer(text="Remember: You don't need to do everything perfectly! Pick what feels most helpful for you right now. 💝")
        
        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot):
    bot.tree.add_command(HabitCommands(bot)) 