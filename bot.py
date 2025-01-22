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
from contextlib import asynccontextmanager
from functools import wraps
from typing import Optional
import sys

# Initialize colorama for Windows support
colorama.init()

class ConfigurationError(Exception):
    """Raised when there's an issue with the bot's configuration."""
    pass

class Configuration:
    """Manages bot configuration with validation and defaults."""
    
    def __init__(self):
        load_dotenv()
        
        # Required settings
        self.token = self._get_required('DISCORD_TOKEN', 'Discord bot token is required')
        
        # Optional settings with defaults
        self.reminder_channel = self._get_optional('REMINDER_CHANNEL_ID')
        self.db_path = self._get_optional('DB_PATH', 'gentle_habits.db')
        self.max_db_connections = int(self._get_optional('MAX_DB_CONNECTIONS', '5'))
        self.streak_update_interval = int(self._get_optional('STREAK_UPDATE_INTERVAL', '5'))  # minutes
        self.log_level = self._get_optional('LOG_LEVEL', 'INFO')
        
    def _get_required(self, key: str, message: str) -> str:
        """Get a required configuration value."""
        value = os.getenv(key)
        if not value:
            raise ConfigurationError(message)
        return value
        
    def _get_optional(self, key: str, default: Optional[str] = None) -> str:
        """Get an optional configuration value with default."""
        return os.getenv(key, default)

# Global configuration instance
try:
    config = Configuration()
except ConfigurationError as e:
    print(f"Configuration Error: {e}")
    sys.exit(1)

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
TOKEN = os.getenv('DISCORD_TOKEN')
REMINDER_CHANNEL_ID = os.getenv('REMINDER_CHANNEL_ID')

# Add a custom exception class for better error handling
class HabitError(Exception):
    """Base exception class for habit-related errors"""
    pass

class HabitNotFoundError(HabitError):
    """Raised when a habit cannot be found"""
    pass

class InvalidTimeFormatError(HabitError):
    """Raised when time format is invalid"""
    pass

class DatabasePool:
    def __init__(self, db_path: str, max_connections: int = 5):
        self.db_path = db_path
        self.max_connections = max_connections
        self._pool = asyncio.Queue(maxsize=max_connections)
        self._connections = 0

    async def _create_connection(self):
        """Create a new database connection."""
        connection = await aiosqlite.connect(self.db_path)
        return connection

    async def initialize(self):
        """Initialize the connection pool."""
        for _ in range(self.max_connections):
            connection = await self._create_connection()
            await self._pool.put(connection)
            self._connections += 1

    async def close(self):
        """Close all connections in the pool."""
        while not self._pool.empty():
            connection = await self._pool.get()
            await connection.close()
            self._connections -= 1

    @asynccontextmanager
    async def acquire(self):
        """Acquire a connection from the pool."""
        connection = await self._pool.get()
        try:
            yield connection
        finally:
            await self._pool.put(connection)

class GentleHabitsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.scheduler = None
        self.db_path = config.db_path
        self.db_pool = DatabasePool(self.db_path, config.max_db_connections)
        self.habit_messages = {}
        self.streak_message = None
        
        # Configure logging
        logger.setLevel(getattr(logging, config.log_level.upper()))
        
        # Register event handlers
        self.setup_events()
        
    def setup_events(self):
        """Register event handlers."""
        @self.event
        async def on_ready():
            print(LOGO)
            logger.info(f'🤖 Bot is awakening...')
            logger.info(f'🌟 Connected as {self.user}')
            logger.info(f'🔧 Running Discord.py version: {discord.__version__}')
            
            try:
                synced = await self.tree.sync()
                logger.info(f'✨ Successfully synced {len(synced)} command(s)')
            except Exception as e:
                logger.error(f'❌ Failed to sync commands: {e}')
            
            # Send initial streak board
            try:
                await self.update_streak_board()
                logger.info('📊 Initial streak board sent')
            except Exception as e:
                logger.error(f'❌ Failed to send initial streak board: {e}')
            
            logger.info(f'🎉 Bot is now ready to help build gentle habits!')

    async def setup_hook(self):
        """Initialize bot components."""
        logger.info("Initializing bot components...")
        await self.db_pool.initialize()
        await self.init_db()
        await self.load_extension('commands')
        await self.setup_scheduler()
        logger.info("Bot initialization complete")

    async def close(self):
        """Override close to properly cleanup resources."""
        if self.scheduler:
            self.scheduler.shutdown(wait=True)
        await self.db_pool.close()
        await super().close()
    
    async def init_db(self):
        """Initialize the SQLite database with required tables."""
        async with self.db_pool.acquire() as db:
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
        self.create_scheduler()
        
        # Schedule restock reminders
        self.scheduler.add_job(
            self.check_restock_reminders,
            CronTrigger(hour=9, minute=0),
            id='check_restock'
        )
        
        # Schedule streak board updates
        self.scheduler.add_job(
            self.update_streak_board,
            CronTrigger(minute=f'*/{config.streak_update_interval}'),
            id='update_streaks'
        )
        
        # Set up habit schedules
        async with self.db_pool.acquire() as db:
            async with db.execute('SELECT id, name, reminder_time, expiry_time FROM habits') as cursor:
                async for habit in cursor:
                    await self._schedule_habit(*habit)
        
        self.scheduler.start()
        logger.info("Scheduler initialized with all jobs")
        
    async def _schedule_habit(self, habit_id: int, name: str, reminder_time: str, expiry_time: str):
        """Schedule reminder and expiry for a single habit."""
        try:
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
            logger.debug(f"Scheduled habit {name} (ID: {habit_id})")
        except ValueError as e:
            logger.error(f"Failed to schedule habit {name} (ID: {habit_id}): {e}")
    
    async def send_habit_reminder(self, habit_id: int, habit_name: str):
        """Send a reminder for a specific habit."""
        if not REMINDER_CHANNEL_ID:
            return
            
        channel = self.get_channel(int(REMINDER_CHANNEL_ID))
        if not channel:
            return
            
        # Get users who need to be reminded for this habit
        async with self.db_pool.acquire() as db:
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
            logger.warning("No reminder channel ID set - streak board updates disabled")
            return
            
        try:
            channel = self.get_channel(int(REMINDER_CHANNEL_ID))
            if not channel:
                logger.error(f"Could not find channel with ID {REMINDER_CHANNEL_ID}")
                return

            embed = await self.create_streak_board_embed()
            
            if self.streak_message:
                try:
                    message = await channel.fetch_message(self.streak_message)
                    await message.edit(embed=embed)
                    logger.debug("Successfully updated existing streak board")
                except discord.NotFound:
                    logger.info("Previous streak board message was deleted, creating new one")
                    message = await channel.send(embed=embed)
                    self.streak_message = message.id
                except discord.Forbidden:
                    logger.error("Bot lacks permissions to edit streak board message", exc_info=True)
                    # Try to send a new message as fallback
                    message = await channel.send(embed=embed)
                    self.streak_message = message.id
                    logger.info("Created new streak board message as fallback")
                except discord.HTTPException as e:
                    logger.error(f"Discord API error while updating streak board: {e}", exc_info=True)
                    return
            else:
                message = await channel.send(embed=embed)
                self.streak_message = message.id
                logger.info("Created initial streak board message")
                
        except Exception as e:
            logger.error(f"Critical error in update_streak_board: {e}", exc_info=True)
            # Don't raise the exception - this is a background task
    
    async def create_streak_board_embed(self):
        """Create the streak board embed."""
        embed = discord.Embed(
            title="🏆 Current Streaks",
            description="Everyone's habit journey - every day is a new opportunity! ✨",
            color=discord.Color.gold()
        )
        
        try:
            async with self.db_pool.acquire() as db:
                # First check if there are any habit participants
                cursor = await db.execute('''
                    SELECT COUNT(*) 
                    FROM habit_participants
                ''')
                count = (await cursor.fetchone())[0]
                
                if count == 0:
                    embed.description = "No one has joined any habits yet! Start your journey today! ✨"
                    embed.add_field(
                        name="Get Started",
                        value="Use `/habit create` to begin tracking a new habit!",
                        inline=False
                    )
                    return embed
                
                # Get all streaks with user validation, including 0s
                async with db.execute('''
                    SELECT DISTINCT hp.user_id, h.name, COALESCE(uh.current_streak, 0) as streak
                    FROM habit_participants hp
                    JOIN habits h ON hp.habit_id = h.id
                    LEFT JOIN user_habits uh ON hp.habit_id = uh.habit_id AND hp.user_id = uh.user_id
                    ORDER BY streak DESC, h.name
                    LIMIT 15
                ''') as cursor:
                    valid_entries = 0
                    async for user_id, habit_name, streak in cursor:
                        try:
                            user = await self.fetch_user(user_id)
                            if user:
                                # Customize emoji based on streak
                                if streak > 7:
                                    emoji = "🔥"  # Fire for week+
                                elif streak > 0:
                                    emoji = "⭐"   # Star for active streak
                                else:
                                    emoji = "🌱"   # Seedling for fresh start
                                
                                # Customize message based on streak
                                if streak == 0:
                                    streak_text = "Ready to start!"
                                else:
                                    streak_text = f"{streak} day{'s' if streak != 1 else ''}"
                                
                                embed.add_field(
                                    name=f"{user.display_name} - {habit_name}",
                                    value=f"{emoji} {streak_text}",
                                    inline=False
                                )
                                valid_entries += 1
                            else:
                                # User no longer in server, clean up their entries
                                await db.execute(
                                    'DELETE FROM user_habits WHERE user_id = ?',
                                    (user_id,)
                                )
                                await db.execute(
                                    'DELETE FROM habit_participants WHERE user_id = ?',
                                    (user_id,)
                                )
                        except discord.NotFound:
                            # User no longer exists, clean up their entries
                            await db.execute(
                                'DELETE FROM user_habits WHERE user_id = ?',
                                (user_id,)
                            )
                            await db.execute(
                                'DELETE FROM habit_participants WHERE user_id = ?',
                                (user_id,)
                            )
                        except Exception as e:
                            logger.error(f"Error processing streak for user {user_id}: {e}")
                            continue
                
                await db.commit()
                
                if valid_entries == 0:
                    embed.description = "No active participants found. Start your journey today! ✨"
                    embed.add_field(
                        name="Get Started",
                        value="Use `/habit create` to begin tracking a new habit!",
                        inline=False
                    )
        except Exception as e:
            logger.error(f"Error creating streak board embed: {e}")
            embed.description = "⚠️ Error loading streak data. Please try again later."
            
        embed.set_footer(text="Updated every 5 minutes")
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
        
        async with self.db_pool.acquire() as db:
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

def validate_time_format(time_str: str) -> bool:
    """Validate time string format and reasonable values"""
    try:
        time = datetime.strptime(time_str, "%H:%M")
        # Ensure hours and minutes are within reasonable ranges
        return 0 <= time.hour <= 23 and 0 <= time.minute <= 59
    except ValueError:
        return False

@asynccontextmanager
async def get_db_connection(db_path: str):
    """Async context manager for database connections"""
    async with aiosqlite.connect(db_path) as db:
        try:
            yield db
        except Exception as e:
            await db.rollback()
            raise HabitError(f"Database error: {str(e)}")

def rate_limit(calls: int, period: int):
    """Rate limiting decorator for commands
    calls: number of allowed calls
    period: time period in seconds
    """
    def decorator(func):
        last_reset = datetime.now()
        calls_made = 0
        
        @wraps(func)
        async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
            nonlocal last_reset, calls_made
            
            now = datetime.now()
            if now - last_reset > timedelta(seconds=period):
                calls_made = 0
                last_reset = now
                
            if calls_made >= calls:
                await interaction.response.send_message(
                    "Please wait a moment before using this command again.",
                    ephemeral=True
                )
                return
                
            calls_made += 1
            return await func(self, interaction, *args, **kwargs)
        return wrapper
    return decorator

if __name__ == '__main__':
    logger.info('🚀 Starting Gentle Habits Bot...')
    bot = GentleHabitsBot()
    bot.run(config.token) 