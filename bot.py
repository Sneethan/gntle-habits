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
import json
from utils import get_current_time, convert_to_local, convert_to_utc
import aiohttp

# Initialize colorama for Windows support
colorama.init()

# Import check_deepseek_status from commands
from commands import client, check_deepseek_status

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
        self.timezone = self._get_optional('TIMEZONE', 'UTC')  # Default to UTC if not specified
        self.affirmation_tone = self._get_optional('AFFIRMATION_TONE', 'balanced')  # gentle, balanced, or firm
        
        # Load affirmations from JSON file
        try:
            with open('affirmations.json', 'r', encoding='utf-8') as f:
                self.affirmations = json.load(f)
            if self.affirmation_tone not in self.affirmations:
                logger.warning(f"Invalid affirmation tone '{self.affirmation_tone}', falling back to 'balanced'")
                self.affirmation_tone = 'balanced'
        except FileNotFoundError:
            logger.error("affirmations.json not found")
            self.affirmations = {}
        except json.JSONDecodeError:
            logger.error("Invalid JSON in affirmations.json")
            self.affirmations = {}
        
        # Validate timezone
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(self.timezone)
        except zoneinfo.ZoneInfoNotFoundError:
            logger.warning(f"Invalid timezone '{self.timezone}', falling back to UTC")
            self.timezone = 'UTC'
        
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
                
            # Check for missed reminders after bot is fully ready
            try:
                await self.check_missed_reminders()
                logger.info('🔍 Checked for missed reminders')
            except Exception as e:
                logger.error(f'❌ Failed to check missed reminders: {e}')
            
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
            
            # Create morning briefing preferences table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS morning_briefing_prefs (
                    user_id INTEGER PRIMARY KEY,
                    opted_in BOOLEAN DEFAULT 0,
                    location TEXT,
                    greeting_time TEXT DEFAULT '07:00',
                    created_at TEXT,
                    bus_origin TEXT DEFAULT '85 Bastick Street, Rosny, TAS::-42.872160,147.359686',
                    bus_destination TEXT DEFAULT 'Hobart City Interchange, Hobart, TAS::-42.882473,147.329588'
                )
            ''')
            
            # Create event countdown table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS event_countdowns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    event_name TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    created_at TEXT,
                    include_in_briefing BOOLEAN DEFAULT 1,
                    UNIQUE(user_id, event_name)
                )
            ''')
            
            await db.commit()
    
    def create_scheduler(self):
        """Create a new scheduler instance."""
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)
        
    async def setup_scheduler(self):
        """Set up dynamic schedulers for habits and reminders."""
        self.create_scheduler()
        
        # Schedule restock reminders - run at 9am in configured timezone
        self.scheduler.add_job(
            self.check_restock_reminders,
            CronTrigger(hour=9, minute=0, timezone=config.timezone),
            id='check_restock'
        )
        
        # Schedule streak board updates
        self.scheduler.add_job(
            self.update_streak_board,
            CronTrigger(minute=f'*/{config.streak_update_interval}', timezone=config.timezone),
            id='update_streaks'
        )
        
        # Schedule morning briefings - check every minute
        self.scheduler.add_job(
            self.send_morning_briefing,
            CronTrigger(minute='*', timezone=config.timezone),
            id='morning_briefing'
        )
        
        # Set up habit schedules
        async with self.db_pool.acquire() as db:
            async with db.execute('SELECT id, name, reminder_time, expiry_time FROM habits') as cursor:
                async for habit in cursor:
                    await self._schedule_habit(*habit)
        
        self.scheduler.start()
        logger.info(f"Scheduler initialized with all jobs in {config.timezone} timezone")
        
    async def _schedule_habit(self, habit_id: int, name: str, reminder_time: str, expiry_time: str):
        """Schedule reminder and expiry for a single habit."""
        try:
            # Schedule reminder
            reminder_hour, reminder_minute = map(int, reminder_time.split(':'))
            self.scheduler.add_job(
                self.send_habit_reminder,
                CronTrigger(hour=reminder_hour, minute=reminder_minute, timezone=config.timezone),
                id=f'reminder_{habit_id}',
                args=[habit_id, name]
            )
            
            # Schedule expiry check
            if expiry_time:
                expiry_hour, expiry_minute = map(int, expiry_time.split(':'))
                self.scheduler.add_job(
                    self.check_habit_expiry,
                    CronTrigger(hour=expiry_hour, minute=expiry_minute, timezone=config.timezone),
                    id=f'expiry_{habit_id}',
                    args=[habit_id]
                )
            logger.debug(f"Scheduled habit {name} (ID: {habit_id}) for {reminder_time} {config.timezone}")
        except ValueError as e:
            logger.error(f"Failed to schedule habit {name} (ID: {habit_id}): {e}")
    
    async def send_habit_reminder(self, habit_id: int, habit_name: str, channel: discord.TextChannel = None):
        """Send a reminder for a habit."""
        if not channel and config.reminder_channel:
            channel = self.get_channel(int(config.reminder_channel))
        
        if not channel:
            logger.error(f"Could not find reminder channel for habit {habit_name}")
            return
            
        async with self.db_pool.acquire() as db:
            # Get participants for this habit
            cursor = await db.execute('''
                SELECT user_id 
                FROM habit_participants 
                WHERE habit_id = ?
            ''', (habit_id,))
            participants = await cursor.fetchall()
            
            if not participants:
                logger.warning(f"No participants found for habit {habit_name}")
                return
                
            # Create the reminder embed
            embed = discord.Embed(
                title=f"✨ Time for: {habit_name}",
                description="It's time to work on your habit! Take it one small step at a time.",
                color=discord.Color.green()
            )
            
            # Mention all participants
            mentions = ' '.join(f'<@{participant[0]}>' for participant in participants)
            
            # Create the button view
            view = HabitButton(habit_id)
            
            # Send the reminder
            try:
                message = await channel.send(content=mentions, embed=embed, view=view)
                self.habit_messages[habit_id] = message.id  # Store message ID instead of message object
                logger.info(f"Sent reminder for habit {habit_name} to {len(participants)} participants")
            except Exception as e:
                logger.error(f"Failed to send reminder for habit {habit_name}: {str(e)}")
    
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
            title="<:sparkle_star:1333765410608119818> Current Streaks",
            description="Here's how you're doing!",
            color=discord.Color.from_rgb(249, 226, 175)
        )
        
        try:
            async with self.db_pool.acquire() as db:
                try:
                    await db.execute('BEGIN TRANSACTION')
                    
                    # Clean up any invalid streaks (negative values)
                    await db.execute('''
                        UPDATE user_habits 
                        SET current_streak = 1 
                        WHERE current_streak < 0
                    ''')
                    
                    # Clean up orphaned streak records
                    await db.execute('''
                        DELETE FROM user_habits 
                        WHERE habit_id NOT IN (SELECT id FROM habits)
                    ''')
                    
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
                        await db.commit()
                        return embed
                    
                    # Get all streaks with user validation, including 0s
                    # Also include the last check-in time for validation
                    async with db.execute('''
                        SELECT DISTINCT 
                            hp.user_id, 
                            h.name, 
                            COALESCE(uh.current_streak, 0) as streak,
                            uh.last_check_in,
                            h.expiry_time,
                            h.id as habit_id
                        FROM habit_participants hp
                        JOIN habits h ON hp.habit_id = h.id
                        LEFT JOIN user_habits uh 
                            ON hp.habit_id = uh.habit_id 
                            AND hp.user_id = uh.user_id
                        ORDER BY streak DESC, h.name
                        LIMIT 15
                    ''') as cursor:
                        valid_entries = 0
                        now = get_current_time()
                        today = now.date()
                        
                        async for user_id, habit_name, streak, last_check_in, expiry_time, habit_id in cursor:
                            try:
                                user = await self.fetch_user(user_id)
                                if user:
                                    # Validate streak based on last check-in
                                    if last_check_in:
                                        last_check = convert_to_local(datetime.fromisoformat(last_check_in))
                                        days_since_check = (today - last_check.date()).days
                                        
                                        # If past expiry time and no check-in today, count as missed
                                        if expiry_time and days_since_check == 0:
                                            current_time = now.time()
                                            expiry_time_obj = datetime.strptime(expiry_time, "%H:%M").time()
                                            if current_time > expiry_time_obj:
                                                days_since_check = 1
                                        
                                        # Reset streak if more than 1 day has passed
                                        if days_since_check > 1:
                                            streak = 0
                                            await db.execute(
                                                '''UPDATE user_habits 
                                                   SET current_streak = 0 
                                                   WHERE user_id = ? AND habit_id = ?''',
                                                (user_id, habit_id)
                                            )
                                    
                                    # Customize emoji based on streak and status
                                    if streak > 30:
                                        emoji = "<:fire:1333765377364066384>"  # Fire for month+
                                    elif streak > 7:
                                        emoji = "<:fire:1333765377364066384>"  # Fire for week+
                                    elif streak > 0:
                                        emoji = "<:starstreak:1333765612769509459>"  # Active streak
                                    else:
                                        emoji = "<:streak_empty:1333765397769490514>"  # Fresh start
                                    
                                    # Customize message based on streak
                                    if streak == 0:
                                        streak_text = "Ready to start!"
                                    else:
                                        streak_text = f"{streak} day{'s' if streak != 1 else ''}"
                                        if streak in [7, 30, 100, 365]:
                                            streak_text += " 🎉"
                                    
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
                    
                    if valid_entries == 0:
                        embed.description = "No active participants found. Start your journey today! ✨"
                        embed.add_field(
                            name="Get Started",
                            value="Use `/habit create` to begin tracking a new habit!",
                            inline=False
                        )
                    
                    await db.commit()
                    
                except Exception as e:
                    logger.error(f"Database error in create_streak_board_embed: {e}")
                    await db.rollback()
                    raise
                    
        except Exception as e:
            logger.error(f"Error creating streak board embed: {e}")
            embed.description = "⚠️ Error loading streak data. Please try again later."
        
        # Add last update time in configured timezone
        now = get_current_time()
        embed.set_footer(text=f"Updated at {now.strftime('%I:%M %p')} {config.timezone}")
        return embed

    async def check_restock_reminders(self):
        """Check for restocks that need reminders."""
        try:
            logger.info("Checking for restock reminders...")
            now = get_current_time()
            three_days_from_now = now + timedelta(days=3)
            
            # Format dates for comparison
            today = now.date().isoformat()
            three_days = three_days_from_now.date().isoformat()
            
            # Find users with restocks due soon
            async with self.db_pool.acquire() as db:
                query = '''
                    SELECT 
                        ri.user_id, ri.item_name, ri.refill_date, 
                        julianday(ri.refill_date) - julianday(?) as days_left
                    FROM 
                        restock_items ri 
                    WHERE 
                        ri.refill_date BETWEEN ? AND ?
                    ORDER BY 
                        ri.refill_date
                '''
                
                cursor = await db.execute(query, (today, today, three_days))
                restock_items = await cursor.fetchall()
                
                # Group by user for fewer notifications
                restock_by_user = {}
                for user_id, item_name, refill_date, days_left in restock_items:
                    if user_id not in restock_by_user:
                        restock_by_user[user_id] = []
                    
                    days_left = int(days_left)
                    refill_date_dt = datetime.fromisoformat(refill_date)
                    
                    restock_by_user[user_id].append({
                        'item_name': item_name,
                        'refill_date': refill_date_dt.strftime('%Y-%m-%d'),
                        'days_left': days_left
                    })
            
            # Send restock reminders to each user
            for user_id, items in restock_by_user.items():
                try:
                    user = self.get_user(user_id)
                    if not user:
                        user = await self.fetch_user(user_id)
                    
                    if user:
                        # Create embed for restock reminder
                        embed = discord.Embed(
                            title="🔔 Restock Reminder",
                            description="The following items will need restocking soon:",
                            color=discord.Color.orange()
                        )
                        
                        # Add items to the embed
                        for item in items:
                            days_text = "TODAY" if item['days_left'] == 0 else f"{item['days_left']} days"
                            embed.add_field(
                                name=item['item_name'],
                                value=f"📅 Restock by: {item['refill_date']} ({days_text})",
                                inline=False
                            )
                        
                        # Add footer with instructions
                        embed.set_footer(text="Use /habit restock-done when you've restocked an item")
                        
                        # Send DM to the user
                        await user.send(embed=embed)
                        logger.info(f"Sent restock reminder to {user.name} for {len(items)} items")
                    else:
                        logger.warning(f"Could not find user with ID {user_id} for restock reminder")
                except Exception as e:
                    logger.error(f"Error sending restock reminder to user {user_id}: {str(e)}")
        
        except Exception as e:
            logger.error(f"Error in check_restock_reminders: {str(e)}")
            
    async def send_morning_briefing(self):
        """Send morning briefings to opted-in users."""
        try:
            now = get_current_time()
            current_time = now.strftime("%H:%M")
            
            # Find users who have opted in for morning briefings and whose greeting time matches current time
            async with self.db_pool.acquire() as db:
                cursor = await db.execute(
                    '''SELECT user_id, location 
                       FROM morning_briefing_prefs 
                       WHERE opted_in = 1 AND greeting_time = ?''',
                    (current_time,)
                )
                users = await cursor.fetchall()
                
                if not users:
                    return  # No users to send briefings to at this time
                
                logger.info(f"Sending morning briefings to {len(users)} users at {current_time}")
                
                for user_data in users:
                    user_id, location = user_data
                    try:
                        # Find the user's Discord object
                        user = self.get_user(user_id)
                        if not user:
                            user = await self.fetch_user(user_id)
                        
                        if user:
                            # Generate and send the briefing
                            await self._send_user_briefing(user, location)
                            logger.info(f"Sent morning briefing to {user.name} (ID: {user_id})")
                        else:
                            logger.warning(f"Could not find user with ID {user_id} for morning briefing")
                    except Exception as e:
                        logger.error(f"Error sending morning briefing to user {user_id}: {str(e)}")
                
        except Exception as e:
            logger.error(f"Error in send_morning_briefing: {str(e)}")
            
    async def _send_user_briefing(self, user, location):
        """Generate and send a morning briefing to a specific user."""
        try:
            now = get_current_time()
            
            # Get additional user preferences
            async with self.db_pool.acquire() as db:
                cursor = await db.execute(
                    '''SELECT bus_origin, bus_destination 
                       FROM morning_briefing_prefs 
                       WHERE user_id = ?''',
                    (user.id,)
                )
                user_prefs = await cursor.fetchone()
                bus_origin = user_prefs[0] if user_prefs else None
                bus_destination = user_prefs[1] if user_prefs else None
            
            # Create embed for the briefing
            embed = discord.Embed(
                title=f"Good Morning, {user.display_name}! 🌅",
                description=f"It's {now.strftime('%A, %B %d, %Y')}. Here's your morning briefing:",
                color=discord.Color.gold()
            )
            
            # Add weather information if location is provided
            if location:
                weather_info = await self._get_weather_info(location)
                if weather_info:
                    embed.add_field(
                        name="📊 Weather",
                        value=weather_info,
                        inline=False
                    )
            
            # Add bus transit information if origin and destination are provided
            if bus_origin and bus_destination:
                bus_info = await self._get_bus_info(bus_origin, bus_destination)
                if bus_info:
                    embed.add_field(
                        name="🚌 Next Bus",
                        value=bus_info,
                        inline=False
                    )
            
            # Add restock reminders
            restock_info = await self._get_restock_info(user.id)
            if restock_info:
                embed.add_field(
                    name="📦 Restock Reminders",
                    value=restock_info,
                    inline=False
                )
            
            # Add event countdowns
            countdown_info = await self._get_event_countdowns(user.id)
            if countdown_info:
                embed.add_field(
                    name="📅 Event Countdowns",
                    value=countdown_info,
                    inline=False
                )
            
            # Add footer
            embed.set_footer(text="Have a wonderful day! Use /briefing commands to customize your briefing.")
            
            # Send the briefing DM
            await user.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error generating briefing for {user.name}: {str(e)}")
            
    async def _get_weather_info(self, location):
        """Get weather information for the specified location."""
        try:
            # OpenWeatherMap API call
            api_key = os.getenv('OPENWEATHERMAP_API_KEY')
            if not api_key:
                return "Weather information unavailable (API key not configured)"
            
            # Make API call to OpenWeatherMap
            async with aiohttp.ClientSession() as session:
                url = f"https://api.openweathermap.org/data/2.5/weather?q={location}&appid={api_key}&units=metric"
                async with session.get(url) as response:
                    if response.status != 200:
                        return f"Weather information unavailable (Error: {response.status})"
                    
                    data = await response.json()
                    
                    # Extract relevant weather data
                    weather_description = data['weather'][0]['description']
                    temp_current = data['main']['temp']
                    temp_feels_like = data['main']['feels_like']
                    humidity = data['main']['humidity']
                    wind_speed = data['wind']['speed']
                    
                    # Convert wind speed from m/s to km/h for easier understanding
                    wind_speed_kmh = wind_speed * 3.6  # 1 m/s = 3.6 km/h
                    
                    # Get descriptive text for weather conditions
                    humidity_desc = self._get_humidity_description(humidity)
                    wind_desc = self._get_wind_description(wind_speed_kmh)
                    
                    # Get clothing recommendations from DeepSeek
                    clothing_advice = await self._get_clothing_advice(data)
                    
                    weather_text = (
                        f"**{location}**: {weather_description.capitalize()}\n"
                        f"🌡️ Temperature: {temp_current:.1f}°C (feels like {temp_feels_like:.1f}°C)\n"
                        f"💧 Humidity: {humidity}% - {humidity_desc}\n"
                        f"💨 Wind: {wind_speed_kmh:.1f} km/h - {wind_desc}\n\n"
                        f"**Suggestion**: {clothing_advice}"
                    )
                    
                    return weather_text
                    
        except Exception as e:
            logger.error(f"Error fetching weather: {str(e)}")
            return "Weather information unavailable"
            
    def _get_humidity_description(self, humidity):
        """Convert numerical humidity value to descriptive text."""
        if humidity < 30:
            return "Very dry"
        elif humidity < 40:
            return "Dry"
        elif humidity < 60:
            return "Comfortable"
        elif humidity < 70:
            return "Moderate"
        elif humidity < 80:
            return "Humid"
        else:
            return "Very humid"
            
    def _get_wind_description(self, wind_speed_kmh):
        """Convert numerical wind speed to descriptive text based on Beaufort scale."""
        if wind_speed_kmh < 1:
            return "Calm"
        elif wind_speed_kmh < 6:
            return "Light air"
        elif wind_speed_kmh < 12:
            return "Light breeze"
        elif wind_speed_kmh < 20:
            return "Gentle breeze"
        elif wind_speed_kmh < 29:
            return "Moderate breeze"
        elif wind_speed_kmh < 39:
            return "Fresh breeze"
        elif wind_speed_kmh < 50:
            return "Strong breeze"
        elif wind_speed_kmh < 62:
            return "High wind"
        elif wind_speed_kmh < 75:
            return "Gale"
        elif wind_speed_kmh < 89:
            return "Strong gale"
        elif wind_speed_kmh < 103:
            return "Storm"
        else:
            return "Violent storm"
            
    async def _get_clothing_advice(self, weather_data):
        """Use DeepSeek to generate clothing recommendations based on weather."""
        try:
            deepseek_api_key = os.getenv('DEEPSEEK_API_KEY')
            if not deepseek_api_key:
                return "No specific clothing recommendations available."
            
            # Check if DeepSeek API is available
            api_available, error_msg = await check_deepseek_status()
            if not api_available:
                return "Clothing recommendations unavailable."
            
            # Format weather data for DeepSeek
            weather_prompt = (
                f"Weather: {weather_data['weather'][0]['description']}, "
                f"Temperature: {weather_data['main']['temp']}°C (feels like {weather_data['main']['feels_like']}°C), "
                f"Humidity: {weather_data['main']['humidity']}%, "
                f"Wind: {weather_data['wind']['speed']} m/s"
            )
            
            # Get clothing recommendation from DeepSeek
            prompt = (
                f"As a helpful assistant, recommend appropriate clothing for the following weather conditions in a single sentence: {weather_prompt}. "
                f"Make your advice practical and specific to the weather conditions. Keep it under 100 characters."
            )
            
            completion = await client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=120
            )
            
            recommendation = completion.choices[0].message.content.strip()
            return recommendation
            
        except Exception as e:
            logger.error(f"Error getting clothing advice: {str(e)}")
            return "No specific clothing recommendations available."
            
    async def _get_bus_info(self, origin=None, destination=None):
        """Get real-time bus transit information."""
        try:
            # Use default values if not provided
            if not origin:
                origin = "85 Bastick Street, Rosny, TAS::-42.872160,147.359686"
            if not destination:
                destination = "Hobart City Interchange, Hobart, TAS::-42.882473,147.329588"
            
            # Check if origin and destination have coordinates, if not, geocode them
            if not self._has_coordinates(origin):
                origin = await self._geocode_address(origin)
                if not origin:
                    return "Bus information unavailable (Could not geocode origin address)"
            
            if not self._has_coordinates(destination):
                destination = await self._geocode_address(destination)
                if not destination:
                    return "Bus information unavailable (Could not geocode destination address)"
                    
            logger.info(f"Fetching bus info for route: {origin} → {destination}")
            
            # Define the transit API URL based on metro_tas_request.md
            base_url = "https://otp.transitkit.com.au/directions"
            
            # Parameters from the request template
            current_time = int(datetime.now().timestamp())
            params = {
                "router": "metrotas",
                "origin": origin,
                "destination": destination,
                "departure_time": str(current_time),  # Current time
                "alternatives": "true",
                "key": "AIzaSyAmyAj5G3Rp9df1CBrvBa7dniwMnsrjodY"
            }
            
            # Headers from the request template
            headers = {
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9,en-AU;q=0.8",
                "sec-ch-ua": "\"Not(A:Brand\";v=\"99\", \"Microsoft Edge\";v=\"133\", \"Chromium\";v=\"133\"",
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": "\"Windows\"",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "cross-site",
                "referrer": "https://www.metrotas.com.au/",
                "referrerPolicy": "strict-origin-when-cross-origin"
            }
            
            # Make API call to get transit information
            async with aiohttp.ClientSession() as session:
                logger.debug(f"Making transit API request with params: {params}")
                async with session.get(base_url, params=params, headers=headers) as response:
                    if response.status != 200:
                        logger.error(f"Transit API returned status code {response.status}")
                        return f"Bus information unavailable (API error: {response.status})"
                    
                    data = await response.json()
                    
                    if data.get('status') != 'OK':
                        error_msg = data.get('error_message', 'Unknown error')
                        logger.error(f"Transit API error: {error_msg}")
                        return f"Bus information unavailable: {error_msg}"
                    
                    if not data.get('routes'):
                        logger.warning("Transit API returned no routes")
                        return "No bus routes found between these locations"
                    
                    now = datetime.now().timestamp()
                    upcoming_buses = []
                    
                    # Find upcoming buses
                    for route_idx, route in enumerate(data['routes']):
                        for leg in route.get('legs', []):
                            # Make sure we have departure_time and it's in the future
                            if 'departure_time' not in leg or leg['departure_time'].get('value', 0) <= now:
                                continue
                                
                            departure_time = leg['departure_time']['value']
                            
                            # Try to get the route name from transit details if available
                            route_name = f"Route {route_idx + 1}"
                            for step in leg.get('steps', []):
                                if 'transit_details' in step:
                                    transit_details = step['transit_details']
                                    if 'line' in transit_details and 'short_name' in transit_details['line']:
                                        route_name = transit_details['line']['short_name']
                                    break
                            
                            upcoming_buses.append({
                                'route': route_name,
                                'departure_time': departure_time,
                                'departure_text': leg['departure_time']['text'],
                                'start': leg.get('start_address', 'Unknown'),
                                'end': leg.get('end_address', 'Unknown'),
                                'duration': leg.get('duration', {}).get('text', 'Unknown')
                            })
                    
                    if not upcoming_buses:
                        logger.warning("No upcoming buses found in API response")
                        return "No upcoming buses found. Try checking a different time or route."
                    
                    # Sort by departure time and take the next 3 buses
                    upcoming_buses.sort(key=lambda x: x['departure_time'])
                    upcoming_buses = upcoming_buses[:3]  # Get up to 3 buses
                    
                    # Format the bus information
                    bus_info = []
                    
                    for i, bus in enumerate(upcoming_buses):
                        discord_timestamp = f"<t:{int(bus['departure_time'])}:R>"
                        
                        # Simplify addresses for better readability
                        start_simple = self._simplify_address(bus['start'])
                        end_simple = self._simplify_address(bus['end'])
                        
                        if i == 0:
                            # More detailed info for the next bus
                            bus_info.append(
                                f"**Next Bus: {bus['route']}**\n"
                                f"📍 From **{start_simple}** to **{end_simple}**\n"
                                f"🕒 Departs at {bus['departure_text']} ({discord_timestamp})\n"
                                f"⏱️ Duration: {bus['duration']}"
                            )
                        else:
                            # Simpler format for later buses
                            bus_info.append(
                                f"**{bus['route']}**: Departs {discord_timestamp}, Duration: {bus['duration']}"
                            )
                    
                    logger.info(f"Found {len(upcoming_buses)} upcoming buses")
                    return "\n\n".join(bus_info)
                    
        except Exception as e:
            logger.error(f"Error getting bus info: {str(e)}", exc_info=True)
            return f"Bus information unavailable. Error: {str(e)}"

    def _simplify_address(self, address_string):
        """Simplify long addresses to make them more readable."""
        try:
            if not address_string or address_string == "Unknown":
                return "Unknown Location"
                
            # Remove common long formats and verbose components
            address = address_string.split(',')
            
            # If we have a very short address already, just return it
            if len(address) == 1:
                return address_string
                
            # For location names, focus on the suburb/locality
            # Usually the second part is the suburb/locality
            if len(address) >= 2:
                location = address[1].strip()
                
                # If it's a landmark or common place, it might be in the first part
                first_part = address[0].strip()
                if "Interchange" in first_part or "Mall" in first_part or "Centre" in first_part or "Center" in first_part:
                    location = first_part
                
                # If location part is too short, it might be just a postal code - use the first part instead
                if len(location) <= 5 and len(address) >= 3:
                    location = address[2].strip()
                    
                # Add the state if available for context
                for part in address:
                    part = part.strip()
                    if part in ["TAS", "NSW", "QLD", "VIC", "SA", "WA", "NT", "ACT"]:
                        if not location.endswith(part):
                            location += f", {part}"
                        break
                
                return location
            
            # Fallback if we can't extract a good location name
            return address_string
            
        except Exception as e:
            logger.warning(f"Error simplifying address: {str(e)}")
            return address_string  # Return original if processing fails

    def _has_coordinates(self, location_string):
        """Check if the location string already has coordinates in the format 'name::lat,lon'."""
        return '::' in location_string and ',' in location_string.split('::')[1]

    async def _geocode_address(self, address):
        """Convert an address to coordinates using Google Maps Geocoding API."""
        try:
            # Use OpenStreetMap Nominatim as it doesn't require an API key
            # Note: For production, consider using a geocoding service with an API key for better reliability
            geocoding_url = f"https://nominatim.openstreetmap.org/search"
            
            params = {
                "q": address,
                "format": "json",
                "limit": 1,
                "addressdetails": 1
            }
            
            headers = {
                "User-Agent": "GentleHabitsBot/1.0"  # Required by Nominatim
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(geocoding_url, params=params, headers=headers) as response:
                    if response.status != 200:
                        logger.error(f"Geocoding API returned status code {response.status}")
                        return None
                    
                    data = await response.json()
                    
                    if not data:
                        logger.warning(f"No geocoding results for address: {address}")
                        return None
                    
                    # Extract latitude and longitude
                    lat = data[0].get('lat')
                    lon = data[0].get('lon')
                    display_name = data[0].get('display_name')
                    
                    if not lat or not lon:
                        logger.warning(f"Missing coordinates in geocoding result for: {address}")
                        return None
                    
                    # Format as required by the transit API: 'name::lat,lon'
                    formatted_location = f"{display_name}::{lat},{lon}"
                    logger.info(f"Geocoded '{address}' to '{formatted_location}'")
                    
                    return formatted_location
                    
        except Exception as e:
            logger.error(f"Error geocoding address '{address}': {str(e)}", exc_info=True)
            return None
            
    async def _get_restock_info(self, user_id):
        """Get restock reminders for the user."""
        try:
            async with self.db_pool.acquire() as db:
                cursor = await db.execute(
                    '''SELECT item_name, refill_date FROM restock_items 
                       WHERE user_id = ? 
                       ORDER BY refill_date''',
                    (user_id,)
                )
                items = await cursor.fetchall()
                
                if not items:
                    return None
                
                now = get_current_time()
                
                restock_text = []
                for item_name, refill_date in items:
                    refill_dt = datetime.fromisoformat(refill_date)
                    days_left = (refill_dt.date() - now.date()).days
                    
                    if days_left <= 0:
                        status = "🚨 **RESTOCK NOW**"
                    elif days_left <= 3:
                        status = f"⚠️ **{days_left} day{'s' if days_left != 1 else ''} left**"
                    else:
                        status = f"✅ {days_left} day{'s' if days_left != 1 else ''} left"
                    
                    restock_text.append(f"{item_name}: {status}")
                
                return "\n".join(restock_text) if restock_text else None
                
        except Exception as e:
            logger.error(f"Error getting restock info: {str(e)}")
            return "Restock information unavailable"
            
    async def _get_event_countdowns(self, user_id):
        """Get event countdowns for the user."""
        try:
            async with self.db_pool.acquire() as db:
                cursor = await db.execute(
                    '''SELECT event_name, event_date FROM event_countdowns 
                       WHERE user_id = ? AND include_in_briefing = 1 
                       ORDER BY event_date''',
                    (user_id,)
                )
                events = await cursor.fetchall()
                
                if not events:
                    return None
                
                now = get_current_time()
                
                countdown_text = []
                for event_name, event_date in events:
                    event_dt = datetime.fromisoformat(event_date)
                    days_left = (event_dt.date() - now.date()).days
                    
                    if days_left < 0:
                        continue  # Skip past events
                    elif days_left == 0:
                        status = "🎉 **TODAY!**"
                    elif days_left == 1:
                        status = "⏰ **TOMORROW!**"
                    else:
                        status = f"📆 {days_left} days"
                    
                    # Add Discord timestamp
                    discord_timestamp = f"<t:{int(event_dt.timestamp())}:R>"
                    countdown_text.append(f"**{event_name}**: {status} ({discord_timestamp})")
                
                return "\n".join(countdown_text) if countdown_text else None
                
        except Exception as e:
            logger.error(f"Error getting event countdowns: {str(e)}")
            return "Event countdown information unavailable"
        
    async def get_reminder_channel(self) -> Optional[discord.TextChannel]:
        """Get the reminder channel, with proper error handling."""
        if not config.reminder_channel:
            logger.warning("No reminder channel configured")
            return None
            
        # Wait for cache to be ready
        if not self.is_ready():
            await self.wait_until_ready()
            
        channel = self.get_channel(int(config.reminder_channel))
        if not channel:
            logger.error(f"Could not find reminder channel {config.reminder_channel}")
            return None
            
        return channel

    async def check_missed_reminders(self):
        """Check for reminders that should have been sent while the bot was offline."""
        try:
            now = get_current_time()
            one_hour_ago = now - timedelta(hours=1)
            
            # Format times for SQL comparison
            now_time = now.strftime("%H:%M")
            one_hour_ago_time = one_hour_ago.strftime("%H:%M")
            
            # Get habits that should have been reminded in the last hour
            async with self.db_pool.acquire() as db:
                query = '''
                    SELECT id, name, reminder_time, expiry_time FROM habits 
                    WHERE reminder_time BETWEEN ? AND ?
                '''
                
                # Handle case where the range crosses midnight
                if one_hour_ago_time > now_time:
                    cursor = await db.execute(
                        'SELECT id, name, reminder_time, expiry_time FROM habits WHERE reminder_time >= ? OR reminder_time <= ?',
                        (one_hour_ago_time, now_time)
                    )
                else:
                    cursor = await db.execute(query, (one_hour_ago_time, now_time))
                
                habits = await cursor.fetchall()
                
                # Get a reference to the reminder channel
                channel = await self.get_reminder_channel()
                if not channel:
                    logger.error("Could not find reminder channel for missed reminder check")
                    return
                
                for habit in habits:
                    habit_id, name, reminder_time, expiry_time = habit
                    
                    # Parse times into datetime objects for comparison
                    reminder_dt = datetime.strptime(reminder_time, "%H:%M").replace(
                        year=now.year, month=now.month, day=now.day, 
                        tzinfo=now.tzinfo
                    )
                    
                    # Adjust if the reminder was yesterday
                    if reminder_dt > now:
                        reminder_dt -= timedelta(days=1)
                    
                    expiry_dt = datetime.strptime(expiry_time, "%H:%M").replace(
                        year=now.year, month=now.month, day=now.day,
                        tzinfo=now.tzinfo
                    )
                    
                    # Adjust if expiry is tomorrow
                    if expiry_dt < reminder_dt:
                        expiry_dt += timedelta(days=1)
                    
                    # Only send if not expired
                    if now < expiry_dt:
                        logger.info(f"Sending catch-up reminder for habit: {name}")
                        await self.send_habit_reminder(habit_id, name, channel)
                    else:
                        logger.info(f"Skipping expired catch-up reminder for habit: {name}")
                        
        except Exception as e:
            logger.error(f"Error in check_missed_reminders: {str(e)}")

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