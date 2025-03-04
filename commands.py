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
import logging

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
            title="🌟 Gentle Habits Bot Commands",
            description="Here are all the available commands to help you build gentle habits.",
            color=discord.Color.from_rgb(186, 225, 255)
        )
        
        # Habit tracking commands
        embed.add_field(
            name="📝 Habit Tracking",
            value=(
                "`/habit create` - Create a new habit to track\n"
                "`/habit list` - List all your habits and streaks\n"
                "`/habit delete` - Delete a habit\n"
                "`/habit edit` - Edit an existing habit\n"
                "`/habit gentle-nudge` - Get a gentle reminder of tasks"
            ),
            inline=False
        )
        
        # Task & Motivation commands
        embed.add_field(
            name="✨ Task Assistance",
            value=(
                "`/habit break-down` - Break a task into smaller steps\n"
                "`/habit organise` - Organize multiple tasks in ADHD-friendly way\n"
                "`/habit motivate` - Get encouragement for a task\n"
                "`/habit energy-check` - Match tasks to your energy level\n"
                "`/habit setup-space` - Get a checklist for optimal environment"
            ),
            inline=False
        )
        
        # Reminders & Tracking
        embed.add_field(
            name="⏰ Reminders & Tools",
            value=(
                "`/habit timer` - Set a Pomodoro-style timer\n"
                "`/habit restock-add` - Track items for restocking\n"
                "`/habit restock-done` - Mark an item as restocked\n"
                "`/habit celebrate` - Record your achievements\n"
                "`/habit celebration-history` - View past celebrations\n"
                "`/habit event` - Create a Discord event"
            ),
            inline=False
        )
        
        # Morning Briefing commands
        embed.add_field(
            name="🌅 Morning Briefings",
            value=(
                "`/briefing opt-in` - Subscribe to morning briefings\n"
                "`/briefing opt-out` - Unsubscribe from briefings\n"
                "`/briefing set-location` - Set your weather location\n"
                "`/briefing set-time` - Set your briefing time\n"
                "`/briefing set-bus-origin` - Set your bus journey start point\n"
                "`/briefing set-bus-destination` - Set your bus journey end point\n"
                "`/briefing countdown-add` - Add event countdown\n"
                "`/briefing countdown-list` - View your countdowns\n"
                "`/briefing countdown-remove` - Remove a countdown\n"
                "`/briefing status` - Check your briefing settings\n"
                "`/briefing test` - Send a test briefing"
            ),
            inline=False
        )
        
        # Add footer
        embed.set_footer(text="We're here to help you build habits gently. One small step at a time! 💖")
        
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

    @app_commands.command(name="event", description="Create a Discord event for habit-related activities")
    @app_commands.describe(
        name="Name of the event",
        description="Description of the event",
        start_time="When the event starts (YYYY-MM-DD HH:MM)",
        duration="Duration in minutes",
        event_type="Type of event",
        location="Where the event will take place (optional)"
    )
    @app_commands.choices(event_type=[
        app_commands.Choice(name="Group Habit Session 👥", value="group_session"),
        app_commands.Choice(name="Celebration 🎉", value="celebration"),
        app_commands.Choice(name="Check-in Meeting 💫", value="check_in"),
        app_commands.Choice(name="Accountability Session 🤝", value="accountability"),
        app_commands.Choice(name="Gentle Planning 📝", value="planning")
    ])
    async def create_event(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str,
        start_time: str,
        duration: int,
        event_type: app_commands.Choice[str],
        location: str = None
    ):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Parse the start time
            start = datetime.strptime(start_time, "%Y-%m-%d %H:%M")
            end = start + timedelta(minutes=duration)
            
            # Ensure the event is in the future
            if start < datetime.now():
                await interaction.followup.send(
                    "Event start time must be in the future!",
                    ephemeral=True
                )
                return
            
            # Create event metadata based on type
            event_metadata = {
                "group_session": {
                    "emoji": "👥",
                    "description_prefix": "Join us for a group habit-building session!\n\n"
                },
                "celebration": {
                    "emoji": "🎉",
                    "description_prefix": "Let's celebrate our progress together!\n\n"
                },
                "check_in": {
                    "emoji": "💫",
                    "description_prefix": "Time for a gentle check-in on our habits.\n\n"
                },
                "accountability": {
                    "emoji": "🤝",
                    "description_prefix": "Support each other in building healthy habits!\n\n"
                },
                "planning": {
                    "emoji": "📝",
                    "description_prefix": "Let's plan our habit journey together.\n\n"
                }
            }
            
            metadata = event_metadata[event_type.value]
            
            # Create the event
            event = await interaction.guild.create_scheduled_event(
                name=f"{metadata['emoji']} {name}",
                description=f"{metadata['description_prefix']}{description}\n\n💝 This is a gentle, supportive space for everyone.",
                start_time=start,
                end_time=end,
                location=location if location else "Voice Channels",
                entity_type=discord.EntityType.voice if not location else discord.EntityType.external
            )
            
            # Create response embed
            embed = discord.Embed(
                title="✨ Event Created!",
                description=f"Your {event_type.name} has been scheduled.",
                color=discord.Color.green()
            )
            
            embed.add_field(
                name="📅 Event Details",
                value=f"**Name:** {event.name}\n"
                      f"**When:** <t:{int(start.timestamp())}:F>\n"
                      f"**Duration:** {duration} minutes\n"
                      f"**Where:** {location if location else 'Voice Channels'}",
                inline=False
            )
            
            embed.add_field(
                name="📝 Description",
                value=event.description,
                inline=False
            )
            
            # Add link to the event
            embed.add_field(
                name="🔗 Event Link",
                value=f"[Click to view event]({event.url})",
                inline=False
            )
            
            embed.set_footer(text="Remember: Everyone is welcome, and there's no pressure to perform! 💝")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except ValueError:
            await interaction.followup.send(
                "Please use the format YYYY-MM-DD HH:MM for the start time (e.g., 2024-03-25 15:30)",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to create events in this server!",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"An error occurred while creating the event: {str(e)}",
                ephemeral=True
            )

class BriefingCommands(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="briefing", description="Morning briefing commands")
        self.bot = bot
        
    @app_commands.command(name="opt-in", description="Opt in to receive morning briefings")
    @app_commands.describe(
        greeting_time="Time for daily briefing (HH:MM in 24h format)",
        location="Your location for weather information (e.g., 'London, UK')"
    )
    async def opt_in(
        self,
        interaction: discord.Interaction,
        greeting_time: str,
        location: str = None
    ):
        # Validate time format
        try:
            datetime.strptime(greeting_time, "%H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Please use HH:MM format for time (e.g., 07:00, 08:30)",
                ephemeral=True
            )
            return
            
        async with aiosqlite.connect(self.bot.db_path) as db:
            # Check if user already has preferences
            cursor = await db.execute(
                'SELECT user_id FROM morning_briefing_prefs WHERE user_id = ?',
                (interaction.user.id,)
            )
            user_exists = await cursor.fetchone()
            
            if user_exists:
                # Update existing preferences
                await db.execute(
                    '''UPDATE morning_briefing_prefs 
                       SET opted_in = 1, greeting_time = ?, location = ?
                       WHERE user_id = ?''',
                    (greeting_time, location, interaction.user.id)
                )
            else:
                # Insert new preferences
                await db.execute(
                    '''INSERT INTO morning_briefing_prefs 
                       (user_id, opted_in, greeting_time, location, created_at)
                       VALUES (?, 1, ?, ?, ?)''',
                    (interaction.user.id, greeting_time, location, datetime.now().isoformat())
                )
                
            await db.commit()
            
            response = [
                "✨ You've been subscribed to morning briefings!",
                f"📅 Your briefing will arrive at {greeting_time} each day."
            ]
            
            if location:
                response.append(f"🌍 Weather information will be for: {location}")
            else:
                response.append("ℹ️ No location set - weather information won't be included.")
                
            response.append("You can opt out any time with `/briefing opt-out`.")
            
            await interaction.response.send_message("\n".join(response), ephemeral=True)
            
    @app_commands.command(name="opt-out", description="Opt out of morning briefings")
    async def opt_out(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.bot.db_path) as db:
            await db.execute(
                'UPDATE morning_briefing_prefs SET opted_in = 0 WHERE user_id = ?',
                (interaction.user.id,)
            )
            await db.commit()
            
            await interaction.response.send_message(
                "You've been unsubscribed from morning briefings. You can opt in again anytime with `/briefing opt-in`.",
                ephemeral=True
            )
            
    @app_commands.command(name="set-location", description="Update your location for weather forecasts")
    @app_commands.describe(location="Your location (e.g., 'New York, US', 'London, UK')")
    async def set_location(self, interaction: discord.Interaction, location: str):
        async with aiosqlite.connect(self.bot.db_path) as db:
            # Check if user already has preferences
            cursor = await db.execute(
                'SELECT user_id FROM morning_briefing_prefs WHERE user_id = ?',
                (interaction.user.id,)
            )
            user_exists = await cursor.fetchone()
            
            if user_exists:
                # Update existing location
                await db.execute(
                    'UPDATE morning_briefing_prefs SET location = ? WHERE user_id = ?',
                    (location, interaction.user.id)
                )
            else:
                # Create new preferences with default time
                await db.execute(
                    '''INSERT INTO morning_briefing_prefs 
                       (user_id, opted_in, location, greeting_time, created_at)
                       VALUES (?, 0, ?, '07:00', ?)''',
                    (interaction.user.id, location, datetime.now().isoformat())
                )
                
            await db.commit()
            
            await interaction.response.send_message(
                f"📍 Your location has been updated to: {location}\n"
                "Weather information will be included in your briefings.",
                ephemeral=True
            )
            
    @app_commands.command(name="set-time", description="Update your morning briefing time")
    @app_commands.describe(greeting_time="Time for daily briefing (HH:MM in 24h format)")
    async def set_time(self, interaction: discord.Interaction, greeting_time: str):
        # Validate time format
        try:
            datetime.strptime(greeting_time, "%H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Please use HH:MM format for time (e.g., 07:00, 08:30)",
                ephemeral=True
            )
            return
            
        async with aiosqlite.connect(self.bot.db_path) as db:
            # Check if user already has preferences
            cursor = await db.execute(
                'SELECT user_id FROM morning_briefing_prefs WHERE user_id = ?',
                (interaction.user.id,)
            )
            user_exists = await cursor.fetchone()
            
            if user_exists:
                # Update existing time
                await db.execute(
                    'UPDATE morning_briefing_prefs SET greeting_time = ? WHERE user_id = ?',
                    (greeting_time, interaction.user.id)
                )
            else:
                # Create new preferences
                await db.execute(
                    '''INSERT INTO morning_briefing_prefs 
                       (user_id, opted_in, greeting_time, created_at)
                       VALUES (?, 0, ?, ?)''',
                    (interaction.user.id, greeting_time, datetime.now().isoformat())
                )
                
            await db.commit()
            
            await interaction.response.send_message(
                f"⏰ Your briefing time has been updated to: {greeting_time}",
                ephemeral=True
            )
            
    @app_commands.command(name="status", description="Check your current briefing settings")
    async def status(self, interaction: discord.Interaction):
        async with self.bot.db_pool.acquire() as db:
            cursor = await db.execute(
                '''SELECT opted_in, location, greeting_time, bus_origin, bus_destination
                   FROM morning_briefing_prefs 
                   WHERE user_id = ?''',
                (interaction.user.id,)
            )
            user_prefs = await cursor.fetchone()
            
            if not user_prefs:
                embed = discord.Embed(
                    title="Briefing Status",
                    description="You have not set up morning briefings yet.",
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name="Get Started",
                    value="Use `/briefing opt-in` to set up your morning briefings.",
                    inline=False
                )
            else:
                opted_in, location, greeting_time, bus_origin, bus_destination = user_prefs
                
                # Parse bus strings to extract readable info
                origin_info = "Not set"
                if bus_origin:
                    parts = bus_origin.split('::')
                    if parts and len(parts) > 0:
                        origin_info = parts[0]
                        
                destination_info = "Not set"
                if bus_destination:
                    parts = bus_destination.split('::')
                    if parts and len(parts) > 0:
                        destination_info = parts[0]
                
                status_text = "**Active** ✅" if opted_in else "**Inactive** ❌"
                embed = discord.Embed(
                    title="Briefing Status",
                    description=f"Your morning briefing is currently {status_text}",
                    color=discord.Color.green() if opted_in else discord.Color.red()
                )
                
                # Add fields for each setting
                embed.add_field(
                    name="Greeting Time",
                    value=greeting_time,
                    inline=True
                )
                
                embed.add_field(
                    name="Weather Location",
                    value=location or "Not set",
                    inline=True
                )
                
                embed.add_field(
                    name="Bus Origin",
                    value=origin_info,
                    inline=True
                )
                
                embed.add_field(
                    name="Bus Destination",
                    value=destination_info,
                    inline=True
                )
                
                # Count active countdowns
                cursor = await db.execute(
                    '''SELECT COUNT(*) FROM event_countdowns 
                       WHERE user_id = ? AND include_in_briefing = 1''',
                    (interaction.user.id,)
                )
                countdown_count = (await cursor.fetchone())[0]
                
                embed.add_field(
                    name="Event Countdowns",
                    value=f"{countdown_count} active",
                    inline=True
                )
                
                # Add instructions based on current status
                if not opted_in:
                    embed.add_field(
                        name="Activate Briefings",
                        value="Use `/briefing opt-in` to activate your morning briefings.",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="Manage Briefings",
                        value="Use the `/briefing` commands to update your preferences or opt out.",
                        inline=False
                    )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
    @app_commands.command(name="countdown-add", description="Add an event countdown to your morning briefings")
    @app_commands.describe(
        event_name="Name of the event to count down to",
        event_date="Date of the event (YYYY-MM-DD)",
        include_in_briefing="Whether to include this in your morning briefings"
    )
    async def countdown_add(
        self,
        interaction: discord.Interaction,
        event_name: str,
        event_date: str,
        include_in_briefing: bool = True
    ):
        # Validate date format
        try:
            parsed_date = datetime.strptime(event_date, "%Y-%m-%d")
            # Convert to ISO format with time
            event_datetime = parsed_date.replace(hour=0, minute=0, second=0).isoformat()
        except ValueError:
            await interaction.response.send_message(
                "Please use YYYY-MM-DD format for date (e.g., 2023-12-31)",
                ephemeral=True
            )
            return
            
        async with aiosqlite.connect(self.bot.db_path) as db:
            try:
                # Insert the countdown
                await db.execute(
                    '''INSERT INTO event_countdowns 
                       (user_id, event_name, event_date, include_in_briefing, created_at)
                       VALUES (?, ?, ?, ?, ?)''',
                    (interaction.user.id, event_name, event_datetime, include_in_briefing, datetime.now().isoformat())
                )
                await db.commit()
                
                # Calculate days until event
                now = datetime.now().date()
                days_until = (parsed_date.date() - now).days
                
                if days_until < 0:
                    days_text = "This event is in the past"
                elif days_until == 0:
                    days_text = "This event is TODAY!"
                elif days_until == 1:
                    days_text = "This event is TOMORROW!"
                else:
                    days_text = f"This event is in {days_until} days"
                
                # Create response
                response = [
                    f"✨ Added countdown: **{event_name}**",
                    f"📅 Date: {event_date}",
                    f"⏱️ {days_text}"
                ]
                
                if include_in_briefing:
                    response.append("🔔 This countdown will be included in your morning briefings")
                else:
                    response.append("🔕 This countdown will NOT be included in your morning briefings")
                    
                await interaction.response.send_message("\n".join(response), ephemeral=True)
                
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(
                    f"You already have an event named '{event_name}'. Please use a different name or delete the existing one first.",
                    ephemeral=True
                )
                
    @app_commands.command(name="countdown-list", description="List all your event countdowns")
    async def countdown_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.bot.db_path) as db:
            cursor = await db.execute(
                '''SELECT event_name, event_date, include_in_briefing 
                   FROM event_countdowns 
                   WHERE user_id = ? 
                   ORDER BY event_date''',
                (interaction.user.id,)
            )
            events = await cursor.fetchall()
            
            if not events:
                await interaction.response.send_message(
                    "You don't have any event countdowns yet. Add one with `/briefing countdown-add`!",
                    ephemeral=True
                )
                return
                
            # Create embed for countdowns
            embed = discord.Embed(
                title="📅 Your Event Countdowns",
                description="Here are all your upcoming events:",
                color=discord.Color.blue()
            )
            
            now = datetime.now().date()
            
            for event_name, event_date, include_in_briefing in events:
                event_dt = datetime.fromisoformat(event_date).date()
                days_left = (event_dt - now).days
                
                # Format the status based on days left
                if days_left < 0:
                    status = f"🔄 {abs(days_left)} days ago"
                elif days_left == 0:
                    status = "🎉 TODAY!"
                elif days_left == 1:
                    status = "⏰ TOMORROW!"
                else:
                    status = f"📆 {days_left} days left"
                    
                # Create Discord timestamp
                discord_timestamp = f"<t:{int(datetime.fromisoformat(event_date).timestamp())}:D>"
                
                # Add briefing indicator
                briefing_indicator = "🔔" if include_in_briefing else "🔕"
                
                embed.add_field(
                    name=f"{briefing_indicator} {event_name}",
                    value=f"{status}\n📅 {discord_timestamp}",
                    inline=False
                )
                
            embed.set_footer(text="🔔 = Included in briefings | 🔕 = Not in briefings")
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
    @app_commands.command(name="countdown-remove", description="Remove an event countdown")
    @app_commands.describe(event_name="Name of the event to remove")
    async def countdown_remove(self, interaction: discord.Interaction, event_name: str):
        async with aiosqlite.connect(self.bot.db_path) as db:
            # Check if the event exists
            cursor = await db.execute(
                'SELECT event_name FROM event_countdowns WHERE user_id = ? AND event_name = ?',
                (interaction.user.id, event_name)
            )
            event = await cursor.fetchone()
            
            if not event:
                await interaction.response.send_message(
                    f"No event named '{event_name}' was found. Please check the name and try again.",
                    ephemeral=True
                )
                return
                
            # Delete the event
            await db.execute(
                'DELETE FROM event_countdowns WHERE user_id = ? AND event_name = ?',
                (interaction.user.id, event_name)
            )
            await db.commit()
            
            await interaction.response.send_message(
                f"✅ Removed countdown for: **{event_name}**",
                ephemeral=True
            )
            
    @app_commands.command(name="test", description="Send a test briefing to yourself")
    async def test_briefing(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(self.bot.db_path) as db:
            cursor = await db.execute(
                'SELECT location FROM morning_briefing_prefs WHERE user_id = ?',
                (interaction.user.id,)
            )
            result = await cursor.fetchone()
            location = result[0] if result else None
        
        # Send a test briefing
        try:
            await self.bot._send_user_briefing(interaction.user, location)
            await interaction.followup.send(
                "✅ Test briefing sent! Check your DMs.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Failed to send test briefing: {str(e)}\n"
                "Make sure you have DMs enabled from server members.",
                ephemeral=True
            )
    
    @app_commands.command(name="set-bus-origin", description="Set your bus journey starting point")
    @app_commands.describe(
        location_name="Nickname for this location (e.g., 'Home', 'Work')",
        address="Full address of the location (e.g., '123 Main St, City, State')"
    )
    async def set_bus_origin(
        self,
        interaction: discord.Interaction,
        location_name: str,
        address: str
    ):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Let the bot handle geocoding
            formatted_address = await self.bot._geocode_address(address)
            
            if not formatted_address:
                await interaction.followup.send(
                    "❌ Could not find coordinates for that address. Please try a more specific address.",
                    ephemeral=True
                )
                return
                
            # Use the original location name with the geocoded address
            bus_origin = formatted_address.split('::')[0]
            bus_origin = f"{location_name}, {bus_origin}::{formatted_address.split('::')[1]}"
            
            # Update the database
            async with self.bot.db_pool.acquire() as db:
                # Check if user has briefing preferences
                cursor = await db.execute(
                    "SELECT user_id FROM morning_briefing_prefs WHERE user_id = ?",
                    (interaction.user.id,)
                )
                user_exists = await cursor.fetchone()
                
                if user_exists:
                    # Update existing record
                    await db.execute(
                        "UPDATE morning_briefing_prefs SET bus_origin = ? WHERE user_id = ?",
                        (bus_origin, interaction.user.id)
                    )
                else:
                    # Create new record with defaults
                    now = datetime.now().isoformat()
                    await db.execute(
                        """INSERT INTO morning_briefing_prefs 
                           (user_id, opted_in, greeting_time, created_at, bus_origin) 
                           VALUES (?, 0, '07:00', ?, ?)""",
                        (interaction.user.id, now, bus_origin)
                    )
                
                await db.commit()
            
            embed = discord.Embed(
                title="Bus Origin Updated",
                description=f"Your bus journey will start from **{location_name}** ({address}).",
                color=discord.Color.green()
            )
            embed.add_field(
                name="Note",
                value="You need to opt in to morning briefings to receive transit information."
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error setting bus origin: {str(e)}",
                ephemeral=True
            )
    
    @app_commands.command(name="set-bus-destination", description="Set your bus journey destination")
    @app_commands.describe(
        location_name="Nickname for this location (e.g., 'Home', 'Work')",
        address="Full address of the location (e.g., '123 Main St, City, State')"
    )
    async def set_bus_destination(
        self,
        interaction: discord.Interaction,
        location_name: str,
        address: str
    ):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Let the bot handle geocoding
            formatted_address = await self.bot._geocode_address(address)
            
            if not formatted_address:
                await interaction.followup.send(
                    "❌ Could not find coordinates for that address. Please try a more specific address.",
                    ephemeral=True
                )
                return
                
            # Use the original location name with the geocoded address
            bus_destination = formatted_address.split('::')[0]
            bus_destination = f"{location_name}, {bus_destination}::{formatted_address.split('::')[1]}"
            
            # Update the database
            async with self.bot.db_pool.acquire() as db:
                # Check if user has briefing preferences
                cursor = await db.execute(
                    "SELECT user_id FROM morning_briefing_prefs WHERE user_id = ?",
                    (interaction.user.id,)
                )
                user_exists = await cursor.fetchone()
                
                if user_exists:
                    # Update existing record
                    await db.execute(
                        "UPDATE morning_briefing_prefs SET bus_destination = ? WHERE user_id = ?",
                        (bus_destination, interaction.user.id)
                    )
                else:
                    # Create new record with defaults
                    now = datetime.now().isoformat()
                    await db.execute(
                        """INSERT INTO morning_briefing_prefs 
                           (user_id, opted_in, greeting_time, created_at, bus_destination) 
                           VALUES (?, 0, '07:00', ?, ?)""",
                        (interaction.user.id, now, bus_destination)
                    )
                
                await db.commit()
            
            embed = discord.Embed(
                title="Bus Destination Updated",
                description=f"Your bus journey will end at **{location_name}** ({address}).",
                color=discord.Color.green()
            )
            embed.add_field(
                name="Note",
                value="You need to opt in to morning briefings to receive transit information."
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error setting bus destination: {str(e)}",
                ephemeral=True
            )

async def setup(bot):
    bot.tree.add_command(HabitCommands(bot))
    bot.tree.add_command(BriefingCommands(bot))
    bot.tree.add_command(DebtCommands(bot))

class DebtCommands(app_commands.Group):
    """Commands for tracking and managing debt accounts."""
    
    def __init__(self, bot):
        super().__init__(name="debt", description="Track and manage debt accounts")
        self.bot = bot
    
    @app_commands.command(name="add", description="Add a new debt account to track")
    @app_commands.describe(
        name="Name of the debt account (e.g., 'Credit Card', 'Student Loan')",
        balance="Current balance on the account",
        interest_rate="Annual interest rate as a percentage (optional)",
        due_date="Next payment due date (YYYY-MM-DD format, optional)",
        description="Optional notes about this debt",
        is_public="Whether to show this debt on the public dashboard"
    )
    async def add_debt(
        self,
        interaction: discord.Interaction,
        name: str,
        balance: float,
        interest_rate: float = None,
        due_date: str = None,
        description: str = None,
        is_public: bool = True
    ):
        # Validate inputs
        if balance < 0:
            await interaction.response.send_message(
                "Balance must be a positive number.",
                ephemeral=True
            )
            return
        
        if interest_rate is not None and interest_rate < 0:
            await interaction.response.send_message(
                "Interest rate cannot be negative.",
                ephemeral=True
            )
            return
        
        # Validate due date format if provided
        if due_date:
            try:
                due_date = datetime.strptime(due_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                await interaction.response.send_message(
                    "Due date must be in YYYY-MM-DD format.",
                    ephemeral=True
                )
                return
        
        try:
            # Insert into database
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with aiosqlite.connect(self.bot.db_path) as db:
                try:
                    await db.execute(
                        '''
                        INSERT INTO debt_accounts (
                            user_id, name, current_balance, initial_balance, 
                            interest_rate, due_date, description, created_at, updated_at, is_public
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            interaction.user.id,
                            name,
                            balance,
                            balance,  # Initial balance is the starting balance
                            interest_rate if interest_rate is not None else 0.0,
                            due_date,
                            description,
                            now,
                            now,
                            is_public
                        )
                    )
                    await db.commit()
                except aiosqlite.IntegrityError:
                    await interaction.response.send_message(
                        f"You already have a debt account named '{name}'.",
                        ephemeral=True
                    )
                    return
            
            await interaction.response.send_message(
                f"Debt account '{name}' added successfully with a balance of ${balance:,.2f}!",
                ephemeral=True
            )
            
            # Update the debt dashboard
            await self.bot.update_debt_dashboard()
            
        except Exception as e:
            logging.error(f"Error adding debt account: {e}")
            await interaction.response.send_message(
                "An error occurred while adding your debt account. Please try again.",
                ephemeral=True
            )
    
    async def account_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for user's debt account names."""
        try:
            # Print debug info
            print(f"Autocomplete called for user {interaction.user.id} with current: '{current}'")
            
            # Get all debt accounts for this user
            async with aiosqlite.connect(self.bot.db_path) as db:
                # First check if the user has any accounts
                cursor = await db.execute(
                    'SELECT COUNT(*) FROM debt_accounts WHERE user_id = ?',
                    (interaction.user.id,)
                )
                count = await cursor.fetchone()
                print(f"User has {count[0]} debt accounts")
                
                if count and count[0] == 0:
                    # User has no accounts, return empty list
                    return []
                
                # Get the accounts
                cursor = await db.execute(
                    'SELECT name FROM debt_accounts WHERE user_id = ? AND name LIKE ?',
                    (interaction.user.id, f"%{current}%")
                )
                accounts = await cursor.fetchall()
                print(f"Found {len(accounts)} matching accounts for user")
            
            # Create choices
            choices = []
            for account in accounts:
                name = account[0]
                choices.append(app_commands.Choice(name=name, value=name))
                if len(choices) >= 25:  # Discord limit
                    break
            
            print(f"Returning {len(choices)} choices for autocomplete")
            return choices
            
        except Exception as e:
            print(f"Error in debt account autocomplete: {str(e)}")
            import traceback
            traceback.print_exc()
            return []
    
    @app_commands.command(name="list", description="List all your debt accounts and balances")
    @app_commands.describe(
        show_private="Whether to include private accounts in the list"
    )
    async def list_debts(self, interaction: discord.Interaction, show_private: bool = True):
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                # Query to get all user's accounts
                query = '''
                    SELECT id, name, current_balance, initial_balance, interest_rate, 
                           due_date, description, is_public
                    FROM debt_accounts 
                    WHERE user_id = ?
                '''
                
                if not show_private:
                    query += ' AND is_public = 1'
                    
                query += ' ORDER BY current_balance DESC'
                
                cursor = await db.execute(query, (interaction.user.id,))
                accounts = await cursor.fetchall()
                
                if not accounts:
                    await interaction.response.send_message(
                        "You don't have any debt accounts yet! Use `/debt add` to create one.",
                        ephemeral=True
                    )
                    return
                
                # Create embed to display accounts
                embed = discord.Embed(
                    title="Your Debt Tracker Accounts",
                    description="Here's a summary of all your debt accounts",
                    color=discord.Color.teal()
                )
                
                total_current = 0
                total_initial = 0
                
                for account in accounts:
                    account_id, name, current, initial, rate, due_date, desc, is_public = account
                    
                    total_current += current
                    total_initial += initial
                    
                    # Calculate percentage paid
                    paid_percentage = 0
                    if initial > 0:
                        paid_percentage = 100 - (current / initial * 100)
                    
                    # Create progress bar
                    progress_bar = self.bot._create_progress_bar(paid_percentage)
                    
                    # Format interest rate display
                    interest_display = f" ({rate}%)" if rate > 0 else ""
                    
                    # Format due date display
                    due_display = f"\nDue: {due_date}" if due_date else ""
                    
                    # Format description
                    desc_display = f"\n{desc}" if desc else ""
                    
                    # Privacy indicator
                    privacy = "🔒 Private" if not is_public else "🌐 Public"
                    
                    embed.add_field(
                        name=f"{name}{interest_display} ({privacy})",
                        value=(
                            f"Balance: `${current:,.2f}/${initial:,.2f}`\n"
                            f"{progress_bar} ({paid_percentage:.1f}% paid)"
                            f"{due_display}{desc_display}"
                        ),
                        inline=False
                    )
                
                # Calculate total progress
                total_percentage = 0
                if total_initial > 0:
                    total_percentage = 100 - (total_current / total_initial * 100)
                    
                total_progress_bar = self.bot._create_progress_bar(total_percentage)
                
                embed.add_field(
                    name="📊 OVERALL PROGRESS",
                    value=(
                        f"Total Debt: `${total_current:,.2f}/${total_initial:,.2f}`\n"
                        f"{total_progress_bar} ({total_percentage:.1f}% paid)"
                    ),
                    inline=False
                )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
        except Exception as e:
            logging.error(f"Error listing debt accounts: {e}")
            await interaction.response.send_message(
                "An error occurred while retrieving your debt accounts. Please try again.",
                ephemeral=True
            )
    
    @app_commands.command(name="payment", description="Record a payment on a debt account")
    @app_commands.describe(
        account_name="Name of the debt account",
        amount="Payment amount",
        date="Payment date (YYYY-MM-DD format, defaults to today)",
        notes="Optional notes about this payment"
    )
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    async def record_payment(
        self,
        interaction: discord.Interaction,
        account_name: str,
        amount: float,
        date: str = None,
        notes: str = None
    ):
        # Validate inputs
        if amount <= 0:
            await interaction.response.send_message(
                "Payment amount must be positive.",
                ephemeral=True
            )
            return
        
        # Validate date format if provided
        payment_date = datetime.now().strftime("%Y-%m-%d")
        if date:
            try:
                payment_date = datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                await interaction.response.send_message(
                    "Date must be in YYYY-MM-DD format.",
                    ephemeral=True
                )
                return
        
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                await db.execute('BEGIN TRANSACTION')
                try:
                    # Get account info
                    cursor = await db.execute(
                        'SELECT id, current_balance FROM debt_accounts WHERE user_id = ? AND name = ?',
                        (interaction.user.id, account_name)
                    )
                    account = await cursor.fetchone()
                    
                    if not account:
                        await db.rollback()
                        await interaction.response.send_message(
                            f"You don't have a debt account named '{account_name}'.",
                            ephemeral=True
                        )
                        return
                        
                    account_id, current_balance = account
                    
                    # Calculate new balance
                    new_balance = current_balance - amount
                    if new_balance < 0:
                        new_balance = 0  # Don't allow negative balances
                    
                    # Record the payment
                    await db.execute(
                        'INSERT INTO debt_payments (account_id, amount, payment_date, description) VALUES (?, ?, ?, ?)',
                        (account_id, amount, payment_date, notes)
                    )
                    
                    # Update the account balance
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    await db.execute(
                        'UPDATE debt_accounts SET current_balance = ?, updated_at = ? WHERE id = ?',
                        (new_balance, now, account_id)
                    )
                    
                    await db.commit()
                    
                    await interaction.response.send_message(
                        f"Payment of ${amount:,.2f} recorded for '{account_name}'! New balance: ${new_balance:,.2f}",
                        ephemeral=True
                    )
                    
                    # Update the debt dashboard
                    await self.bot.update_debt_dashboard()
                    
                except Exception as e:
                    await db.rollback()
                    raise e
                
        except Exception as e:
            logging.error(f"Error recording payment: {e}")
            await interaction.response.send_message(
                "An error occurred while recording your payment. Please try again.",
                ephemeral=True
            )
    
    @app_commands.command(name="edit", description="Edit an existing debt account")
    @app_commands.describe(
        account_name="Name of the debt account to edit",
        new_name="New name for the account (optional)",
        new_balance="New current balance (optional)",
        interest_rate="New interest rate (optional)",
        due_date="New due date (YYYY-MM-DD format, optional)",
        description="New description (optional)",
        is_public="Whether to show this debt on the public dashboard (optional)"
    )
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    async def edit_debt(
        self,
        interaction: discord.Interaction,
        account_name: str,
        new_name: str = None,
        new_balance: float = None,
        interest_rate: float = None,
        due_date: str = None,
        description: str = None,
        is_public: bool = None
    ):
        # Make sure at least one field is being updated
        if all(param is None for param in [new_name, new_balance, interest_rate, due_date, description, is_public]):
            await interaction.response.send_message(
                "You need to provide at least one field to update.",
                ephemeral=True
            )
            return
        
        # Validate inputs
        if new_balance is not None and new_balance < 0:
            await interaction.response.send_message(
                "Balance must be a positive number.",
                ephemeral=True
            )
            return
        
        if interest_rate is not None and interest_rate < 0:
            await interaction.response.send_message(
                "Interest rate cannot be negative.",
                ephemeral=True
            )
            return
        
        # Validate due date format if provided
        if due_date:
            try:
                due_date = datetime.strptime(due_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                await interaction.response.send_message(
                    "Due date must be in YYYY-MM-DD format.",
                    ephemeral=True
                )
                return
        
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                await db.execute('BEGIN TRANSACTION')
                try:
                    # Check if account exists
                    cursor = await db.execute(
                        'SELECT id, current_balance FROM debt_accounts WHERE user_id = ? AND name = ?',
                        (interaction.user.id, account_name)
                    )
                    account = await cursor.fetchone()
                    
                    if not account:
                        await db.rollback()
                        await interaction.response.send_message(
                            f"You don't have a debt account named '{account_name}'.",
                            ephemeral=True
                        )
                        return
                        
                    account_id, current_balance = account
                    
                    # Prepare update query
                    update_fields = []
                    update_values = []
                    
                    if new_name is not None:
                        update_fields.append("name = ?")
                        update_values.append(new_name)
                    
                    if new_balance is not None:
                        update_fields.append("current_balance = ?")
                        update_values.append(new_balance)
                        
                        # Record the balance change
                        balance_diff = new_balance - current_balance
                        if balance_diff != 0:
                            sign = "+" if balance_diff > 0 else ""
                            note = f"Balance manually adjusted by {sign}${abs(balance_diff):,.2f}"
                            
                            now = datetime.now().strftime("%Y-%m-%d")
                            await db.execute(
                                'INSERT INTO debt_payments (account_id, amount, payment_date, description) VALUES (?, ?, ?, ?)',
                                (account_id, -balance_diff, now, note)  # Negative amount because this is an adjustment
                            )
                    
                    if interest_rate is not None:
                        update_fields.append("interest_rate = ?")
                        update_values.append(interest_rate)
                    
                    if due_date is not None:
                        update_fields.append("due_date = ?")
                        update_values.append(due_date)
                    
                    if description is not None:
                        update_fields.append("description = ?")
                        update_values.append(description)
                    
                    if is_public is not None:
                        update_fields.append("is_public = ?")
                        update_values.append(is_public)
                    
                    # Add updated_at field
                    update_fields.append("updated_at = ?")
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    update_values.append(now)
                    
                    # Build and execute the update query
                    if update_fields:
                        query = f"UPDATE debt_accounts SET {', '.join(update_fields)} WHERE id = ?"
                        update_values.append(account_id)
                        await db.execute(query, update_values)
                        
                    await db.commit()
                    
                    # Prepare success message
                    updated_fields = []
                    if new_name is not None:
                        updated_fields.append(f"name to '{new_name}'")
                    if new_balance is not None:
                        updated_fields.append(f"balance to ${new_balance:,.2f}")
                    if interest_rate is not None:
                        updated_fields.append(f"interest rate to {interest_rate}%")
                    if due_date is not None:
                        updated_fields.append(f"due date to {due_date}")
                    if description is not None:
                        updated_fields.append("description")
                    if is_public is not None:
                        updated_fields.append(f"visibility to {'public' if is_public else 'private'}")
                    
                    message = f"Updated {account_name}: " + ", ".join(updated_fields)
                    
                    await interaction.response.send_message(message, ephemeral=True)
                    
                    # Update the debt dashboard
                    await self.bot.update_debt_dashboard()
                    
                except aiosqlite.IntegrityError:
                    await db.rollback()
                    await interaction.response.send_message(
                        f"You already have a debt account with the name '{new_name}'.",
                        ephemeral=True
                    )
                    return
                except Exception as e:
                    await db.rollback()
                    raise e
                
        except Exception as e:
            logging.error(f"Error editing debt account: {e}")
            await interaction.response.send_message(
                "An error occurred while updating your debt account. Please try again.",
                ephemeral=True
            )
    
    @app_commands.command(name="delete", description="Delete a debt account")
    @app_commands.describe(
        account_name="Name of the debt account to delete"
    )
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    async def delete_debt(
        self,
        interaction: discord.Interaction,
        account_name: str,
    ):
        """Delete a debt account and all associated payments."""
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                await db.execute('BEGIN TRANSACTION')
                try:
                    # Check if account exists
                    cursor = await db.execute(
                        'SELECT id FROM debt_accounts WHERE user_id = ? AND name = ?',
                        (interaction.user.id, account_name)
                    )
                    account = await cursor.fetchone()
                    
                    if not account:
                        await db.rollback()
                        await interaction.response.send_message(
                            f"You don't have a debt account named '{account_name}'.",
                            ephemeral=True
                        )
                        return
                    
                    account_id = account[0]
                    
                    # Delete associated payments
                    await db.execute(
                        'DELETE FROM debt_payments WHERE account_id = ?',
                        (account_id,)
                    )
                    
                    # Delete the account
                    await db.execute(
                        'DELETE FROM debt_accounts WHERE id = ?',
                        (account_id,)
                    )
                    
                    await db.commit()
                    
                    await interaction.response.send_message(
                        f"Debt account '{account_name}' and all its payment history have been deleted.",
                        ephemeral=True
                    )
                    
                    # Update the debt dashboard
                    await self.bot.update_debt_dashboard()
                    
                except Exception as e:
                    await db.rollback()
                    raise e
                
        except Exception as e:
            logging.error(f"Error deleting debt account: {e}")
            await interaction.response.send_message(
                "An error occurred while deleting your debt account. Please try again.",
                ephemeral=True
            )
            
    @app_commands.command(name="history", description="View payment history for a debt account")
    @app_commands.describe(
        account_name="Name of the debt account to view history for"
    )
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    async def payment_history(
        self,
        interaction: discord.Interaction,
        account_name: str
    ):
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                # Check if account exists
                cursor = await db.execute(
                    'SELECT id, current_balance, initial_balance FROM debt_accounts WHERE user_id = ? AND name = ?',
                    (interaction.user.id, account_name)
                )
                account = await cursor.fetchone()
                
                if not account:
                    await interaction.response.send_message(
                        f"You don't have a debt account named '{account_name}'.",
                        ephemeral=True
                    )
                    return
                
                account_id, current_balance, initial_balance = account
                
                # Get payment history
                cursor = await db.execute(
                    '''
                    SELECT amount, payment_date, description
                    FROM debt_payments
                    WHERE account_id = ?
                    ORDER BY payment_date DESC
                    LIMIT 15
                    ''',
                    (account_id,)
                )
                payments = await cursor.fetchall()
                
                # Create embed for payment history
                embed = discord.Embed(
                    title=f"Payment History: {account_name}",
                    description=f"Current Balance: ${current_balance:,.2f}\nInitial Balance: ${initial_balance:,.2f}",
                    color=discord.Color.blue()
                )
                
                if not payments:
                    embed.add_field(
                        name="No Payment History",
                        value="No payments have been recorded for this account yet.",
                        inline=False
                    )
                else:
                    for amount, date, description in payments:
                        # Format based on whether it's a payment or adjustment
                        if amount > 0:
                            title = f"💸 Payment: ${amount:,.2f}"
                        else:
                            title = f"🔄 Adjustment: ${-amount:,.2f}"
                        
                        value = f"Date: {date}"
                        if description:
                            value += f"\nNotes: {description}"
                            
                        embed.add_field(
                            name=title,
                            value=value,
                            inline=False
                        )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
        except Exception as e:
            logging.error(f"Error getting payment history: {e}")
            await interaction.response.send_message(
                "An error occurred while retrieving payment history. Please try again.",
                ephemeral=True
            )
    
    @app_commands.command(name="charge", description="Add a charge or fee to increase a debt account balance")
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    @app_commands.describe(
        account_name="Name of the debt account",
        amount="Amount of the charge/fee to add to the debt",
        description="Optional description of what this charge is for"
    )
    async def add_charge(
        self, 
        interaction: discord.Interaction, 
        account_name: str, 
        amount: float, 
        description: str = None
    ):
        """Add a charge or fee to a debt account, increasing its balance."""
        try:
            # Validate inputs
            if amount <= 0:
                await interaction.response.send_message(
                    "The charge amount must be greater than zero.",
                    ephemeral=True
                )
                return
                
            # Find the debt account
            async with aiosqlite.connect(interaction.client.db_path) as db:
                cursor = await db.execute(
                    'SELECT id, current_balance FROM debt_accounts WHERE user_id = ? AND name = ?',
                    (interaction.user.id, account_name)
                )
                account = await cursor.fetchone()
                
                if not account:
                    await interaction.response.send_message(
                        f"You don't have a debt account named '{account_name}'.",
                        ephemeral=True
                    )
                    return
                    
                account_id, current_balance = account
                
                # Calculate the new balance
                new_balance = current_balance + amount
                now = datetime.now().isoformat()
                
                # Record the charge as a negative payment
                await db.execute(
                    'INSERT INTO debt_payments (account_id, amount, payment_date, description) VALUES (?, ?, ?, ?)',
                    (account_id, -amount, now, description or f"Charge/Fee: ${amount:,.2f}")
                )
                
                # Update the current balance
                await db.execute(
                    'UPDATE debt_accounts SET current_balance = ?, updated_at = ? WHERE id = ?',
                    (new_balance, now, account_id)
                )
                
                await db.commit()
                
                # Send confirmation message
                await interaction.response.send_message(
                    f"Added a charge of ${amount:,.2f} to '{account_name}'. New balance: ${new_balance:,.2f}",
                    ephemeral=True
                )
                
                # Update the debt dashboard
                await self.bot.update_debt_dashboard()
                
        except Exception as e:
            logging.error(f"Error adding charge to debt account: {e}")
            await interaction.response.send_message(
                "An error occurred while adding the charge to your debt account. Please try again.",
                ephemeral=True
            )