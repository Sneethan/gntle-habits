import discord
from datetime import datetime
import aiosqlite
import random
import json
import logging
from utils import get_current_time, convert_to_local, convert_to_utc

# Set up logging
logger = logging.getLogger('gentle_habits')

class HabitButton(discord.ui.View):
    def __init__(self, habit_id: int):
        super().__init__(timeout=None)
        self.habit_id = habit_id
        self.add_item(CheckInButton(habit_id))

class CheckInButton(discord.ui.Button):
    def __init__(self, habit_id: int):
        super().__init__(
            label="✨ Check In",
            style=discord.ButtonStyle.green,
            custom_id=f"checkin_{habit_id}"
        )
        self.habit_id = habit_id

    async def callback(self, interaction: discord.Interaction):
        try:
            async with aiosqlite.connect(interaction.client.db_path) as db:
                try:
                    await db.execute('BEGIN TRANSACTION')
                    
                    # Use configured timezone for all datetime operations
                    now = get_current_time()
                    today = now.date()
                    
                    # Get habit info
                    cursor = await db.execute(
                        'SELECT name, expiry_time FROM habits WHERE id = ?',
                        (self.habit_id,)
                    )
                    habit = await cursor.fetchone()
                    if not habit:
                        await interaction.response.send_message(
                            "This habit no longer exists!",
                            ephemeral=True
                        )
                        await db.rollback()
                        return
                    
                    habit_name, expiry_time = habit
                    
                    # Check if we're past the expiry time for today
                    if expiry_time:
                        current_time = now.time()
                        expiry_time_obj = datetime.strptime(expiry_time, "%H:%M").time()
                        if current_time > expiry_time_obj:
                            await interaction.response.send_message(
                                f"Today's check-in window for {habit_name} has expired. Try again tomorrow!",
                                ephemeral=True
                            )
                            await db.rollback()
                            return
                    
                    # Update user's streak with proper timezone handling
                    cursor = await db.execute(
                        '''SELECT current_streak, last_check_in 
                           FROM user_habits 
                           WHERE user_id = ? AND habit_id = ?''',
                        (interaction.user.id, self.habit_id)
                    )
                    row = await cursor.fetchone()
                    
                    if row:
                        current_streak, last_check_in = row
                        if last_check_in:
                            # Convert stored UTC time to local timezone
                            last_check = convert_to_local(datetime.fromisoformat(last_check_in))
                            last_check_date = last_check.date()
                            
                            # Prevent multiple check-ins on the same day
                            if last_check_date == today:
                                await interaction.response.send_message(
                                    f"You've already checked in for {habit_name} today! Keep up the great work! ✨",
                                    ephemeral=True
                                )
                                await db.rollback()
                                return
                            
                            # Calculate days between check-ins
                            days_between = (today - last_check_date).days
                            
                            # Handle streak logic
                            if days_between == 1:  # Perfect streak
                                current_streak += 1
                            elif days_between == 0:  # Same day check-in
                                pass  # Keep current streak
                            else:  # Streak broken
                                current_streak = 1  # Start new streak
                        else:
                            current_streak = 1
                        
                        # Validate streak number
                        if current_streak < 0:
                            current_streak = 1
                        
                        await db.execute(
                            '''UPDATE user_habits 
                               SET current_streak = ?, last_check_in = ? 
                               WHERE user_id = ? AND habit_id = ?''',
                            (current_streak, convert_to_utc(now).isoformat(), interaction.user.id, self.habit_id)
                        )
                    else:
                        current_streak = 1
                        await db.execute(
                            '''INSERT INTO user_habits 
                               (user_id, habit_id, current_streak, last_check_in) 
                               VALUES (?, ?, ?, ?)''',
                            (interaction.user.id, self.habit_id, current_streak, convert_to_utc(now).isoformat())
                        )
                    
                    await db.commit()
                    
                    # Get random affirmation based on configured tone
                    try:
                        with open('affirmations.json', 'r') as f:
                            affirmations = json.load(f)
                        tone = getattr(interaction.client, 'config', None)
                        if tone and hasattr(tone, 'affirmation_tone'):
                            tone = tone.affirmation_tone
                        else:
                            tone = 'balanced'
                        if tone not in affirmations:
                            tone = 'balanced'
                        affirmation = random.choice(affirmations[tone])
                    except (FileNotFoundError, json.JSONDecodeError, KeyError):
                        affirmation = "Great job! 🌟"
                    
                    # Send response with streak milestone celebrations
                    if current_streak == 0:
                        message = f"Starting fresh! Remember, every day is a new opportunity! ✨\n{habit_name} streak: Ready to begin! 🌱"
                    else:
                        message = f"{affirmation}\n{habit_name} streak: {current_streak} day{'s' if current_streak != 1 else ''}! 🔥"
                        
                        # Add milestone celebrations
                        if current_streak in [7, 30, 100, 365]:
                            message += f"\n\n🎉 AMAZING! You've reached a {current_streak}-day streak! 🎉"
                    
                    await interaction.response.send_message(message, ephemeral=True)
                    
                    # Delete the reminder message
                    try:
                        await interaction.message.delete()
                    except discord.NotFound:
                        pass
                        
                except Exception as e:
                    logger.error(f"Error in habit check-in: {e}")
                    await db.rollback()
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            "Something went wrong with your check-in. Please try again!",
                            ephemeral=True
                        )
                    return
        except Exception as e:
            logger.error(f"Critical error in habit check-in: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Something went wrong with your check-in. Please try again!",
                    ephemeral=True
                )
            return

class StreakButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🎯 View Streaks",
            style=discord.ButtonStyle.primary,
            custom_id="view_streaks"
        )

class DailyStreakView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent view
        self.add_item(StreakButton()) 