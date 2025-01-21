import discord
from datetime import datetime
import aiosqlite

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
        async with aiosqlite.connect(interaction.client.db_path) as db:
            now = datetime.now()
            
            # Get habit info
            cursor = await db.execute(
                'SELECT name FROM habits WHERE id = ?',
                (self.habit_id,)
            )
            habit = await cursor.fetchone()
            if not habit:
                await interaction.response.send_message(
                    "This habit no longer exists!",
                    ephemeral=True
                )
                return
            
            habit_name = habit[0]
            
            # Update user's streak
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
                    last_check = datetime.fromisoformat(last_check_in)
                    if (now - last_check).days <= 1:
                        current_streak += 1
                    else:
                        current_streak = 1
                else:
                    current_streak = 1
                
                await db.execute(
                    '''UPDATE user_habits 
                       SET current_streak = ?, last_check_in = ? 
                       WHERE user_id = ? AND habit_id = ?''',
                    (current_streak, now.isoformat(), interaction.user.id, self.habit_id)
                )
            else:
                current_streak = 1
                await db.execute(
                    '''INSERT INTO user_habits 
                       (user_id, habit_id, current_streak, last_check_in) 
                       VALUES (?, ?, ?, ?)''',
                    (interaction.user.id, self.habit_id, current_streak, now.isoformat())
                )
            
            await db.commit()
            
            # Get random affirmation
            cursor = await db.execute('SELECT message FROM affirmations ORDER BY RANDOM() LIMIT 1')
            affirmation = await cursor.fetchone()
            affirmation = affirmation[0] if affirmation else "Great job! 🌟"
            
            # Send ephemeral response
            message = f"{affirmation}\n{habit_name} streak: {current_streak} day{'s' if current_streak != 1 else ''}! 🔥"
            await interaction.response.send_message(message, ephemeral=True)
            
            # Delete the reminder message
            try:
                await interaction.message.delete()
            except discord.NotFound:
                pass

class DailyStreakView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent view
        self.add_item(StreakButton()) 