import os
import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import aiosqlite
from datetime import datetime, timedelta
import random
from dotenv import load_dotenv
import logging
import asyncio
from views import DailyStreakView, HabitButton
import colorama
from colorama import Fore, Style

# Initialize colorama for Windows support
colorama.init()

# ASCII Logo
LOGO = f"""{Fore.GREEN}

   _____            _   _        _    _       _     _ _       
  / ____|          | | | |      | |  | |     | |   (_) |      
 | |  __  ___ _ __ | |_| | ___  | |__| | __ _| |__  _| |_ ___ 
 | | |_ |/ _ \ '_ \| __| |/ _ \ |  __  |/ _` | '_ \| | __/ __|
 | |__| |  __/ | | | |_| |  __/ | |  | | (_| | |_) | | |_\__ 
  \_____|\___|_| |_|\__|_|\___| |_|  |_|\__,_|_.__/|_|\__|___/
                                                              
                                                    
{Style.RESET_ALL}"""

# Set up logging with custom formatting
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'INFO': Fore.CYAN,
        'WARNING': Fore.YELLOW,
        'ERROR': Fore.RED,
        'CRITICAL': Fore.RED + Style.BRIGHT,
        'DEBUG': Fore.BLUE
    }

    def format(self, record):
        color = self.COLORS.get(record.levelname, '')
        record.msg = f"{color}[{record.levelname}]{Style.RESET_ALL} {record.msg}"
        return super().format(record)

# Set up logging
logger = logging.getLogger('gentle_habits')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(handler)

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
REMINDER_CHANNEL_ID = os.getenv('REMINDER_CHANNEL_ID')

class GentleHabitsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.scheduler = None  # Initialize scheduler as None
        self.db_path = 'gentle_habits.db'
        self.habit_messages = {}  # Store message IDs for habit reminders
        self.streak_message = None  # Store message ID for streak board
        
    async def setup_hook(self):
        await self.init_db()
        await self.load_extension('commands')
        await self.setup_scheduler()  # Make setup_scheduler async
        
    async def init_db(self):
        """Initialize the SQLite database with required tables."""
        async with aiosqlite.connect(self.db_path) as db:
            # Create habits table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS habits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    reminder_time TEXT NOT NULL,
                    expiry_time TEXT NOT NULL,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(name)
                )
            ''')
            
            # Create user_habits table for tracking individual progress
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_habits (
                    user_id INTEGER,
                    habit_id INTEGER,
                    current_streak INTEGER DEFAULT 0,
                    last_check_in TEXT,
                    PRIMARY KEY (user_id, habit_id),
                    FOREIGN KEY (habit_id) REFERENCES habits(id)
                )
            ''')
            
            # Create habit_participants table for storing who to ping
            await db.execute('''
                CREATE TABLE IF NOT EXISTS habit_participants (
                    habit_id INTEGER,
                    user_id INTEGER,
                    PRIMARY KEY (habit_id, user_id),
                    FOREIGN KEY (habit_id) REFERENCES habits(id)
                )
            ''')
            
            # Create restock items table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS restock_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    item_name TEXT,
                    refill_date TEXT,
                    days_between_refills INTEGER,
                    UNIQUE(user_id, item_name)
                )
            ''')
            
            # Create affirmations table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS affirmations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT UNIQUE
                )
            ''')
            
            # Insert default affirmations
            default_affirmations = [
                "Great job! You're doing amazing! 🌟",
                "Every small step counts - and you just took one! 🎉",
                "Look at you, taking care of future you! 💪",
                "That's the way! Keep that momentum going! 🚀",
                "You're absolutely crushing it! ✨",
            ]
            
            await db.executemany(
                'INSERT OR IGNORE INTO affirmations (message) VALUES (?)',
                [(msg,) for msg in default_affirmations]
            )
            
            await db.commit()
    
    def create_scheduler(self):
        """Create a new scheduler instance."""
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        self.scheduler = AsyncIOScheduler()
        
    async def setup_scheduler(self):
        """Set up dynamic schedulers for habits and reminders."""
        self.create_scheduler()  # Create fresh scheduler
        
        # Schedule restock reminders
        self.scheduler.add_job(
            self.check_restock_reminders,
            CronTrigger(hour=9, minute=0),
            id='check_restock'
        )
        
        # Schedule streak board updates
        self.scheduler.add_job(
            self.update_streak_board,
            CronTrigger(minute='*/15'),  # Update every 15 minutes
            id='update_streaks'
        )
        
        # Set up habit schedules
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT id, name, reminder_time, expiry_time FROM habits') as cursor:
                async for habit in cursor:
                    habit_id, name, reminder_time, expiry_time = habit
                    # Schedule reminder
                    reminder_hour, reminder_minute = map(int, reminder_time.split(':'))
                    self.scheduler.add_job(
                        self.send_habit_reminder,
                        CronTrigger(hour=reminder_hour, minute=reminder_minute),
                        id=f'reminder_{habit_id}',
                        args=[habit_id, name]
                    )
                    
                    # Schedule expiry check
                    expiry_hour, expiry_minute = map(int, expiry_time.split(':'))
                    self.scheduler.add_job(
                        self.check_habit_expiry,
                        CronTrigger(hour=expiry_hour, minute=expiry_minute),
                        id=f'expiry_{habit_id}',
                        args=[habit_id]
                    )
        
        self.scheduler.start()
    
    async def send_habit_reminder(self, habit_id: int, habit_name: str):
        """Send a reminder for a specific habit."""
        if not REMINDER_CHANNEL_ID:
            return
            
        channel = self.get_channel(int(REMINDER_CHANNEL_ID))
        if not channel:
            return
            
        # Get users who need to be reminded for this habit
        async with aiosqlite.connect(self.db_path) as db:
            # Get all participants for this habit who haven't checked in today
            cursor = await db.execute('''
                SELECT DISTINCT hp.user_id 
                FROM habit_participants hp
                LEFT JOIN user_habits uh 
                    ON hp.habit_id = uh.habit_id 
                    AND hp.user_id = uh.user_id 
                    AND date(uh.last_check_in) = date('now')
                WHERE hp.habit_id = ? 
                    AND (uh.last_check_in IS NULL OR date(uh.last_check_in) != date('now'))
            ''', (habit_id,))
            users = await cursor.fetchall()
            
        if not users:
            return
            
        mentions = " ".join(f"<@{user[0]}>" for user in users)
        content = f"{mentions} Time to check in!"
        embed = discord.Embed(
            title=f"✨ Time for: {habit_name}",
            description="Click the button below to check in!",
            color=discord.Color.green()
        )
        
        view = HabitButton(habit_id)
        message = await channel.send(content=content, embed=embed, view=view)
        self.habit_messages[habit_id] = message.id
    
    async def check_habit_expiry(self, habit_id: int):
        """Check and handle expired habit check-ins."""
        if habit_id in self.habit_messages:
            channel = self.get_channel(int(REMINDER_CHANNEL_ID))
            if channel:
                try:
                    message = await channel.fetch_message(self.habit_messages[habit_id])
                    await message.delete()
                except discord.NotFound:
                    pass
            del self.habit_messages[habit_id]
    
    async def update_streak_board(self):
        """Update the persistent streak board."""
        if not REMINDER_CHANNEL_ID:
            return
            
        channel = self.get_channel(int(REMINDER_CHANNEL_ID))
        if not channel:
            return
            
        embed = await self.create_streak_board_embed()
        
        if self.streak_message:
            try:
                message = await channel.fetch_message(self.streak_message)
                await message.edit(embed=embed)
            except discord.NotFound:
                message = await channel.send(embed=embed)
                self.streak_message = message.id
        else:
            message = await channel.send(embed=embed)
            self.streak_message = message.id
    
    async def create_streak_board_embed(self):
        """Create the streak board embed."""
        embed = discord.Embed(
            title="🏆 Current Streaks",
            description="Keep up the great work everyone!",
            color=discord.Color.gold()
        )
        
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('''
                SELECT u.user_id, h.name, u.current_streak
                FROM user_habits u
                JOIN habits h ON u.habit_id = h.id
                WHERE u.current_streak > 0
                ORDER BY u.current_streak DESC
                LIMIT 10
            ''') as cursor:
                async for user_id, habit_name, streak in cursor:
                    user = await self.fetch_user(user_id)
                    if user:
                        embed.add_field(
                            name=f"{user.display_name} - {habit_name}",
                            value=f"🔥 {streak} day{'s' if streak != 1 else ''}",
                            inline=False
                        )
        
        embed.set_footer(text="Updated every 15 minutes")
        return embed

    async def check_restock_reminders(self):
        """Check and send restock reminders."""
        if not REMINDER_CHANNEL_ID:
            return
            
        channel = self.get_channel(int(REMINDER_CHANNEL_ID))
        if not channel:
            return
            
        today = datetime.now().date()
        reminder_date = today + timedelta(days=3)
        
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'SELECT user_id, item_name FROM restock_items WHERE date(refill_date) = ?',
                (reminder_date.isoformat(),)
            ) as cursor:
                restock_items = []
                async for row in cursor:
                    user_id, item_name = row
                    restock_items.append((user_id, item_name))
                
                if restock_items:
                    embed = discord.Embed(
                        title="🔄 Upcoming Restocks",
                        description="Items that need restocking soon:",
                        color=discord.Color.blue()
                    )
                    
                    for user_id, item_name in restock_items:
                        user = await self.fetch_user(user_id)
                        if user:
                            embed.add_field(
                                name=f"{user.display_name}'s {item_name}",
                                value="Needs restocking in 3 days",
                                inline=False
                            )
                    
                    await channel.send(embed=embed)

bot = GentleHabitsBot()

@bot.event
async def on_ready():
    print(LOGO)
    logger.info(f'🤖 Bot is awakening...')
    logger.info(f'🌟 Connected as {bot.user}')
    logger.info(f'🔧 Running Discord.py version: {discord.__version__}')
    
    try:
        synced = await bot.tree.sync()
        logger.info(f'✨ Successfully synced {len(synced)} command(s)')
    except Exception as e:
        logger.error(f'❌ Failed to sync commands: {e}')
    
    logger.info(f'🎉 Bot is now ready to help build gentle habits!')

if __name__ == '__main__':
    if not TOKEN:
        logger.critical('❌ No Discord token found. Please set the DISCORD_TOKEN environment variable.')
        raise ValueError("No Discord token found. Please set the DISCORD_TOKEN environment variable.")
    
    logger.info('🚀 Starting Gentle Habits Bot...')
    bot.run(TOKEN) 