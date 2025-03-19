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
import urllib.parse
import time

from openai import AsyncOpenAI
from assets.views.views import DailyStreakView, HabitButton, DebtTrackerView
import colorama
from colorama import Fore, Style
from contextlib import asynccontextmanager
from functools import wraps
from typing import Optional
import sys
import json
from assets.utils.utils import get_current_time, convert_to_local, convert_to_utc
import aiohttp
import traceback

# Initialize colorama for Windows support
colorama.init()

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
        self.db_path = self._get_optional('DB_PATH', 'assets/database/gentle_habits/gentle_habits.db')
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

# Set up logging - with check to prevent duplicate handlers
logger = logging.getLogger('gentle_habits')
logger.setLevel(logging.INFO)

# Clear existing handlers to prevent duplicates
if logger.hasHandlers():
    logger.handlers.clear()

# Add our handler with the colored formatter
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(handler)

# Prevent propagation to the root logger to avoid duplicate logs
logger.propagate = False

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
        super().__init__(
            command_prefix='!', 
            intents=intents, 
            shard_count=None  # Auto sharding - Discord.py will determine the right number of shards
        )
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
            
            # Log shard information
            if self.shard_count:
                logger.info(f'🔄 This instance is shard #{self.shard_id} of {self.shard_count}')
                logger.info(f'🌐 Shard is handling {len(self.guilds)} servers')
            else:
                logger.info(f'🌐 Bot is connected to {len(self.guilds)} servers')
            
            try:
                # Add error handling for command registration rate limits
                try:
                    synced = await self.tree.sync()
                    logger.info(f'✨ Successfully synced {len(synced)} command(s)')
                except discord.HTTPException as e:
                    if e.status == 429:  # Rate limit error
                        retry_after = e.retry_after if hasattr(e, 'retry_after') else 60
                        logger.warning(f'⚠️ Discord API rate limit hit when syncing commands. Retry after {retry_after} seconds.')
                        logger.info('📌 The bot will continue to function, but new commands may not be available until the rate limit expires.')
                    else:
                        logger.error(f'❌ HTTP error when syncing commands: {e.status} - {e.text}')
                        logger.error(traceback.format_exc())
                except Exception as e:
                    logger.error(f'❌ Failed to sync commands: {e}')
                    logger.error(traceback.format_exc())
            except Exception as e:
                logger.error(f'❌ Error during command registration: {e}')
                logger.error(traceback.format_exc())
                logger.info('📌 The bot will continue to function, but commands may not be available.')
            
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
            
            # Initialize debt tracker dashboard
            try:
                # Add a timeout to prevent hanging
                create_dashboard_task = asyncio.create_task(self.update_debt_dashboard())
                try:
                    await asyncio.wait_for(create_dashboard_task, timeout=15.0)  # 15 second timeout
                    logger.info("💰 Debt tracker dashboard initialized")
                except asyncio.TimeoutError:
                    logger.warning("⏳ Debt tracker dashboard initialization taking too long - will be created later by scheduler")
            except Exception as e:
                logger.error(f"❌ Failed to initialize debt tracker dashboard: {str(e)}")
                logger.error(traceback.format_exc())
            
            logger.info(f'🎉 Bot is now ready to help build gentle habits!')
            
            # Add a summary of the bot status
            server_count = len(self.guilds)
            user_count = sum(guild.member_count for guild in self.guilds)
            
            logger.info(f'📊 Bot Status Summary:')
            logger.info(f'   • Servers: {server_count}')
            logger.info(f'   • Users: {user_count}')
            logger.info(f'   • Sharding: {"Enabled" if self.shard_count else "Disabled"}')
            logger.info(f'   • Database: {self.db_path}')
            logger.info(f'   • Timezone: {config.timezone}')

    async def setup_hook(self):
        """Set up extensions and initialize the database."""
        logger.info("Setting up the bot...")
        
        # Log sharding information
        if self.shard_count:
            logger.info(f"Bot is using {self.shard_count} shards")
            logger.info(f"Current shard ID: {self.shard_id}")
        else:
            logger.info("Bot is running in a single shard")
        
        # Initialize database pool
        await self.db_pool.initialize()
        
        # Initialize database tables
        await self.init_db()
        
        # Initialize habit scheduler
        await self.setup_scheduler()
        
        # Register persistent views first, before trying to create dashboards
        self.add_view(DailyStreakView())
        self.add_view(DebtTrackerView())
        logger.info("Registered persistent views")
        
        logger.info("Bot setup complete.")

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
            
            # Create debt accounts table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS debt_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    current_balance REAL NOT NULL,
                    initial_balance REAL NOT NULL,
                    interest_rate REAL DEFAULT 0.0,
                    due_date TEXT,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_public BOOLEAN DEFAULT 1,
                    UNIQUE(user_id, name)
                )
            ''')
            
            # Create debt payments table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS debt_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    payment_date TEXT NOT NULL,
                    description TEXT,
                    FOREIGN KEY (account_id) REFERENCES debt_accounts(id)
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
        
        # Schedule debt tracker dashboard updates
        self.scheduler.add_job(
            self.update_debt_dashboard,
            CronTrigger(hour="*/4", minute=0, timezone=config.timezone),  # Update every 4 hours
            id='debt_tracker_update'
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
            channel = await self.get_reminder_channel()
            if channel:
                try:
                    message = await channel.fetch_message(self.habit_messages[habit_id])
                    await message.delete()
                except discord.NotFound:
                    pass
            del self.habit_messages[habit_id]
    
    async def update_streak_board(self):
        """Update the persistent streak board."""
        if not config.reminder_channel:
            logger.warning("No reminder channel ID set - streak board updates disabled")
            return
            
        try:
            # Use the improved get_reminder_channel method instead of direct access
            channel = await self.get_reminder_channel()
            if not channel:
                logger.error("Could not access the reminder channel for streak board updates")
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
                        name="🚌 Transit & Traffic Info",
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
        """Get real-time bus transit and traffic information using both Metro TAS API and Google Maps Routes API."""
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
                    return "Transit information unavailable (Could not geocode origin address)"
            
            if not self._has_coordinates(destination):
                destination = await self._geocode_address(destination)
                if not destination:
                    return "Transit information unavailable (Could not geocode destination address)"
                    
            logger.info(f"Fetching transit info for route: {origin} → {destination}")
            
            # Parse coordinates for API calls
            origin_coords = origin.split('::')[1]
            destination_coords = destination.split('::')[1]
            
            # Create origin and destination from coordinates
            origin_lat, origin_lng = origin_coords.split(',')
            dest_lat, dest_lng = destination_coords.split(',')
            
            # Get API key from environment
            google_maps_api_key = os.getenv('GOOGLE_MAPS_API_KEY')
            if not google_maps_api_key:
                logger.error("GOOGLE_MAPS_API_KEY not found in environment variables")
                return "Transit information unavailable (API key missing)"
                
            logger.debug(f"API key first 5 chars: {google_maps_api_key[:5]}...")
            
            # PART 1: Get bus information from Metro TAS API
            # Format the URL-encoded origin and destination
            origin_name = origin.split('::')[0]
            destination_name = destination.split('::')[0]
            
            # Create properly formatted origin and destination strings with coordinates
            origin_for_api = f"{origin_name}::{origin_coords}"
            destination_for_api = f"{destination_name}::{destination_coords}"
            
            # URL encode the entire strings
            origin_encoded = urllib.parse.quote(origin_for_api)
            destination_encoded = urllib.parse.quote(destination_for_api)
            
            # Get current timestamp for departure_time
            current_timestamp = int(time.time())
            # Add 5 minutes to ensure it's in the future
            future_timestamp = current_timestamp + (5 * 60)
            
            # Construct Metro TAS API URL
            metro_tas_url = f"https://otp.transitkit.com.au/directions?router=metrotas&origin={origin_encoded}&destination={destination_encoded}&departure_time={future_timestamp}&alternatives=true&key={google_maps_api_key}"
            
            # Make request to Metro TAS API
            transit_data = None
            async with aiohttp.ClientSession() as session:
                try:
                    logger.debug(f"Requesting Metro TAS API: {metro_tas_url[:100]}...")
                    async with session.get(metro_tas_url) as response:
                        response_text = await response.text()
                        if response.status == 200:
                            try:
                                transit_data = json.loads(response_text)
                                logger.debug(f"Metro TAS API response status: {response.status}, found {len(transit_data.get('routes', []))} routes")
                            except json.JSONDecodeError:
                                logger.error(f"Failed to parse Metro TAS API response: {response_text[:200]}...")
                        else:
                            logger.error(f"Metro TAS API returned status code {response.status}: {response_text[:200]}...")
                except Exception as e:
                    logger.error(f"Error fetching data from Metro TAS API: {str(e)}")
                    logger.debug(f"Metro TAS URL attempted: {metro_tas_url[:100]}...")
            
            # PART 2: Get traffic information from Google Maps API
            # Define the Google Maps Routes API URL
            routes_url = "https://routes.googleapis.com/directions/v2:computeRoutes"
            
            # Format RFC 3339 timestamp for Google Maps API
            now = datetime.now()
            # Add a small offset to ensure the time is in the future (5 minutes)
            future_time = now + timedelta(minutes=5)
            # Format as RFC 3339 with timezone offset
            formatted_time = future_time.strftime("%Y-%m-%dT%H:%M:%S")
            
            # Add timezone offset if missing
            if "+" not in formatted_time and "-" not in formatted_time[-6:]:
                try:
                    # Try to get local timezone offset
                    offset = future_time.astimezone().strftime('%z')
                    if offset:
                        # Insert colon in timezone offset (e.g., +0000 to +00:00)
                        if len(offset) == 5:
                            offset = f"{offset[:3]}:{offset[3:]}"
                        formatted_time += offset
                    else:
                        formatted_time += "Z"  # UTC if we can't determine local
                except:
                    formatted_time += "Z"  # UTC as fallback
            
            logger.debug(f"Using departure time: {formatted_time}")
            
            # Create payload for driving info to get traffic analysis
            driving_payload = {
                "origin": {
                    "location": {
                        "latLng": {
                            "latitude": float(origin_lat),
                            "longitude": float(origin_lng)
                        }
                    }
                },
                "destination": {
                    "location": {
                        "latLng": {
                            "latitude": float(dest_lat),
                            "longitude": float(dest_lng)
                        }
                    }
                },
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE",
                "departureTime": formatted_time,
                "computeAlternativeRoutes": False,
                "languageCode": "en-US",
                "units": "METRIC"
            }
            
            # Headers for the request
            driving_headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": google_maps_api_key,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs.duration,routes.legs.distanceMeters,routes.travelAdvisory"
            }
            
            # Make API call to get driving information
            driving_data = None
            async with aiohttp.ClientSession() as session:
                logger.debug("Trying Google Maps Routes API for driving data")
                try:
                    async with session.post(routes_url, json=driving_payload, headers=driving_headers) as driving_response:
                        driving_status = driving_response.status
                        driving_text = await driving_response.text()
                        logger.debug(f"Driving API Response: Status {driving_status}, Body: {driving_text[:200]}...")
                        
                        if driving_status == 200:
                            driving_data = json.loads(driving_text)
                        else:
                            logger.error(f"Google Maps Routes API (driving) returned status code {driving_status}: {driving_text}")
                except Exception as e:
                    logger.error(f"Exception with Google Maps API (driving): {str(e)}")
            
            # Process Metro TAS transit information
            transit_info = "No bus routes found between these locations"
            if transit_data and transit_data.get('routes'):
                transit_routes = transit_data.get('routes', [])
                # Extract bus information
                upcoming_buses = []
                
                for route_idx, route in enumerate(transit_routes):
                    legs = route.get('legs', [])
                    if not legs:
                        continue
                    
                    leg = legs[0]  # Get the first leg
                    
                    # Get overall route info
                    departure_time = leg.get('departure_time', {})
                    arrival_time = leg.get('arrival_time', {})
                    duration = leg.get('duration', {})
                    
                    departure_text = departure_time.get('text', 'Unknown time')
                    duration_text = f"{duration.get('text', 'Unknown duration')}"
                    
                    # Extract transit steps
                    steps = leg.get('steps', [])
                    transit_steps = [step for step in steps if step.get('travel_mode') == 'TRANSIT']
                    
                    for step in transit_steps:
                        # Extract transit details
                        transit_details = step.get('transit_details', {})
                        if not transit_details:
                            continue
                        
                        line = transit_details.get('line', {})
                        route_name = line.get('short_name') or line.get('name', f"Route {route_idx + 1}")
                        
                        # Get departure stop
                        departure_stop = transit_details.get('departure_stop', {})
                        stop_name = departure_stop.get('name', 'Unknown stop')
                        
                        # Get walking distance to stop
                        walking_distance = ""
                        for i, step in enumerate(steps):
                            if step.get('travel_mode') == 'TRANSIT' and i > 0 and steps[i-1].get('travel_mode') == 'WALKING':
                                walking_step = steps[i-1]
                                walking_distance = walking_step.get('distance', {}).get('text', '')
                                break
                        
                        # Add to our list of buses
                        upcoming_buses.append({
                            'route': route_name,
                            'departure_time': departure_text,
                            'departure_text': departure_text,
                            'duration': duration_text,
                            'walking_distance': walking_distance,
                            'nearest_stop': stop_name
                        })
                        break  # Just get the first transit step for each route
                
                if upcoming_buses:
                    # Sort by departure time and take the next 3 buses
                    upcoming_buses = upcoming_buses[:3]  # Get up to 3 buses
                    
                    # Format the bus information
                    bus_info = []
                    
                    
                    for i, bus in enumerate(upcoming_buses):
                        if i == 0:
                            # More detailed info for the next bus
                            bus_info.append(
                                f"**Next Bus: {bus['route']}**\n"
                                f"🚏 Stop {i+1}, {bus['nearest_stop']} "
                                f"(Walking distance: {bus['walking_distance'] if bus['walking_distance'] else '0.1 km'})\n"
                                f"🕒 Departs at {bus['departure_text']}\n"
                                f"⏱️ Duration: {bus['duration']}"
                            )
                        else:
                            # Format for subsequent buses - more concise format
                            bus_info.append(
                                f"{bus['route']} from {bus['nearest_stop']}: Departs {bus['departure_text']}, Duration: {bus['duration']}"
                            )
                    
                    transit_info = "\n\n".join(bus_info)
            
            # Process driving information and provide traffic analysis
            traffic_info = ""
            if driving_data and driving_data.get('routes'):
                driving_route = driving_data['routes'][0]
                
                # Get duration including traffic - safely handle the seconds format (e.g., "545s")
                duration_str = driving_route.get('duration', '')
                if isinstance(duration_str, str) and duration_str.endswith('s'):
                    duration_with_traffic = self._format_duration_seconds(duration_str)
                else:
                    # Try the old format just in case
                    duration_obj = driving_route.get('duration', {})
                    duration_with_traffic = duration_obj.get('text', 'Unknown') if isinstance(duration_obj, dict) else str(duration_obj)
                
                # Get distance safely
                distance = driving_route.get('distanceMeters', 0)
                distance_km = distance / 1000
                
                # Get traffic conditions - default to normal if not specified
                traffic_advisory = driving_route.get('travelAdvisory', {}) or {}
                traffic_severity = "normal"
                
                # Log actual traffic advisory structure to help debug
                logger.debug(f"Traffic advisory data: {json.dumps(traffic_advisory, indent=2)}")
                
                # Check if we have any traffic data in the advisory
                if traffic_advisory:
                    # Try to determine traffic severity from available data
                    # The exact field might vary based on the API response structure
                    if 'trafficDensity' in traffic_advisory:
                        traffic_density_value = traffic_advisory.get('trafficDensity')
                        logger.debug(f"Found trafficDensity: {traffic_density_value}")
                        
                        # Routes API traffic density values
                        if traffic_density_value == 'TRAFFIC_DENSITY_HEAVY':
                            traffic_severity = "heavy"
                        elif traffic_density_value == 'TRAFFIC_DENSITY_MEDIUM':
                            traffic_severity = "moderate"
                        elif traffic_density_value == 'TRAFFIC_DENSITY_LOW':
                            traffic_severity = "light"
                    else:
                        # If no specific traffic density field, check for other indicators
                        # such as speed restrictions, road closures, etc.
                        if 'speedReadingIntervals' in traffic_advisory:
                            # If we have speed readings, we might infer traffic from them
                            logger.debug("Found speedReadingIntervals, could analyze for traffic")
                        
                        # For now, default to normal traffic if we can't determine
                        logger.debug("No specific traffic density information found, defaulting to normal")
                
                # Create Google Maps deep link
                origin_for_link = f"{origin_lat},{origin_lng}"
                dest_for_link = f"{dest_lat},{dest_lng}"
                maps_deep_link = f"https://www.google.com/maps/dir/?api=1&origin={origin_for_link}&destination={dest_for_link}&travelmode=driving"
                
                # Prepare traffic description
                if traffic_severity == "heavy":
                    traffic_desc = "🔴 Heavy traffic! Leave extra time for your journey."
                elif traffic_severity == "moderate":
                    traffic_desc = "🟠 Moderate traffic conditions."
                else:
                    traffic_desc = "🟢 Traffic is flowing smoothly."
                
                traffic_info = (
                    f"🚗 **Driving Conditions:**\n"
                    f"{traffic_desc}\n"
                    f"🛣️ Distance: {distance_km:.1f} km\n"
                    f"⏱️ Estimated driving time: {duration_with_traffic}\n"
                    f"📱 [Open in Google Maps]({maps_deep_link})"
                )
            
            # Fall back to just showing transit routes if transit info is available
            if transit_info != "No bus routes found between these locations":
                # Combine transit and traffic information
                if traffic_info:
                    return f"{transit_info}\n\n{traffic_info}"
                else:
                    return transit_info
            # If no transit info is available, but we have traffic, just show that
            elif traffic_info:
                return f"No bus routes available.\n\n{traffic_info}"
            # If both fail, return a generic error message
            else:
                # Provide fallback with Google Maps link
                origin_for_link = f"{origin_lat},{origin_lng}"
                dest_for_link = f"{dest_lat},{dest_lng}"
                maps_deep_link = f"https://www.google.com/maps/dir/?api=1&origin={origin_for_link}&destination={dest_for_link}&travelmode=transit"
                
                return (
                    "🚨 **Transit information unavailable**\n\n"
                    f"📱 You can check routes via [Google Maps]({maps_deep_link})."
                )
                
        except Exception as e:
            logger.error(f"Error fetching transit information: {str(e)}")
            traceback.print_exc()
            
            # Even on exception, try to provide a useful Google Maps link
            try:
                origin_coords = origin.split('::')[1]
                destination_coords = destination.split('::')[1]
                origin_lat, origin_lng = origin_coords.split(',')
                dest_lat, dest_lng = destination_coords.split(',')
                
                origin_for_link = f"{origin_lat},{origin_lng}"
                dest_for_link = f"{dest_lat},{dest_lng}"
                maps_deep_link = f"https://www.google.com/maps/dir/?api=1&origin={origin_for_link}&destination={dest_for_link}&travelmode=transit"
                
                return (
                    "🚨 **Transit information unavailable**\n\n"
                    f"📱 You can check routes via [Google Maps]({maps_deep_link})."
                )
            except:
                pass
                
            return "Transit information unavailable. Please try again later."

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
            logger.info("Waiting for bot to be ready before accessing channels...")
            await self.wait_until_ready()
            logger.info("Bot is now ready, attempting to get reminder channel")
            
        # Try to get the channel by ID
        channel_id = int(config.reminder_channel)
        channel = self.get_channel(channel_id)
        
        # If channel not found in cache, try to fetch it
        if not channel:
            try:
                logger.info(f"Channel {channel_id} not in cache, attempting to fetch it")
                channel = await self.fetch_channel(channel_id)
                logger.info(f"Successfully fetched channel {channel.name} ({channel_id})")
            except discord.NotFound:
                logger.error(f"Channel with ID {channel_id} not found")
                return None
            except discord.Forbidden:
                logger.error(f"Bot does not have permission to access channel with ID {channel_id}")
                return None
            except Exception as e:
                logger.error(f"Could not find reminder channel {channel_id}: {str(e)}")
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

    async def get_debt_tracker_channel(self) -> Optional[discord.TextChannel]:
        """Get the debt tracker channel from the configured ID."""
        channel_id = os.getenv('DEBT_TRACKER_CHANNEL_ID')
        if not channel_id:
            logger.warning("DEBT_TRACKER_CHANNEL_ID not set in environment variables")
            return None
            
        # Wait for cache to be ready
        if not self.is_ready():
            logger.info("Waiting for bot to be ready before accessing debt tracker channel...")
            await self.wait_until_ready()
            logger.info("Bot is now ready, attempting to get debt tracker channel")
            
        try:
            channel_id = int(channel_id)
            channel = self.get_channel(channel_id)
            if not channel:
                try:
                    logger.info(f"Debt tracker channel {channel_id} not in cache, attempting to fetch it")
                    channel = await self.fetch_channel(channel_id)
                    logger.info(f"Successfully fetched debt tracker channel {channel.name} ({channel_id})")
                except discord.NotFound:
                    logger.error(f"Channel with ID {channel_id} not found")
                    return None
                except discord.Forbidden:
                    logger.error(f"Bot doesn't have permission to access channel with ID {channel_id}")
                    return None
                except Exception as e:
                    logger.error(f"Error fetching channel with ID {channel_id}: {str(e)}")
                    return None
            return channel
        except ValueError:
            logger.error(f"Invalid DEBT_TRACKER_CHANNEL_ID: {channel_id} - not a valid integer")
            return None
        except Exception as e:
            logger.error(f"Error getting debt tracker channel: {str(e)}")
            return None
    
    async def update_debt_dashboard(self):
        """Update or create the debt tracker dashboard in the dedicated channel."""
        logger.info("Updating debt tracker dashboard...")
        
        try:
            channel = await self.get_debt_tracker_channel()
            if not channel:
                logger.warning("Debt tracker channel not configured or not found.")
                return
                
            # Create embed dashboard
            embed = await self.create_debt_dashboard_embed()
            
            # Look for existing dashboard message to update with timeout protection
            dashboard_found = False
            try:
                # Use async iteration instead of flatten() for Discord.py v2.x compatibility
                messages = []
                history_task = asyncio.create_task(self._get_recent_messages(channel, 50))
                messages = await asyncio.wait_for(history_task, timeout=15.0)  # 15 second timeout for history fetch
                
                for message in messages:
                    if message.author == self.user and message.embeds:
                        for msg_embed in message.embeds:
                            if msg_embed.title and "Debt Tracker Dashboard" in msg_embed.title:
                                try:
                                    view = DebtTrackerView()
                                    await message.edit(embed=embed, view=view)
                                    logger.info("Debt tracker dashboard updated successfully.")
                                    dashboard_found = True
                                    break
                                except discord.Forbidden:
                                    logger.error("Bot doesn't have permission to edit the debt tracker message.")
                                except discord.HTTPException as e:
                                    logger.error(f"HTTP error updating debt tracker dashboard: {e.status} - {e.text}")
                                except Exception as e:
                                    logger.error(f"Error updating debt tracker dashboard: {str(e)}")
                    
                    if dashboard_found:
                        break
            except asyncio.TimeoutError:
                logger.warning("Timeout while fetching channel history for debt dashboard")
            except Exception as e:
                logger.error(f"Error while searching for existing debt dashboard: {str(e)}")
            
            # No existing message found or update failed, create a new one
            if not dashboard_found:
                try:
                    view = DebtTrackerView()
                    await channel.send(embed=embed, view=view)
                    logger.info("New debt tracker dashboard created.")
                except discord.Forbidden:
                    logger.error("Bot doesn't have permission to send messages in the debt tracker channel.")
                except discord.HTTPException as e:
                    logger.error(f"HTTP error creating debt tracker dashboard: {e.status} - {e.text}")
                except Exception as e:
                    logger.error(f"Error creating debt tracker dashboard: {str(e)}")
        except Exception as e:
            logger.error(f"Error in update_debt_dashboard: {str(e)}")
            logger.error(traceback.format_exc())
            
    async def create_debt_dashboard_embed(self):
        """Create an embed for the debt tracker dashboard."""
        embed = discord.Embed(
            title="🌟 Debt Tracker Dashboard 🌟",
            description="Track your progress in paying down debts. Use the buttons below to update your accounts.",
            color=discord.Color.teal()
        )
        
        embed.add_field(
            name="‎",
            value="**Public Debt Accounts**\n" +
                  "*Everyone can see these accounts, and celebrate progress together!*",
            inline=False
        )
        
        # Get all public debt accounts
        async with self.db_pool.acquire() as db:
            cursor = await db.execute('''
                SELECT 
                    da.id, da.user_id, da.name, da.current_balance, da.initial_balance, 
                    da.interest_rate, da.due_date, da.description, u.user_tag
                FROM debt_accounts da
                LEFT JOIN (
                    SELECT DISTINCT user_id, user_id || '#0000' as user_tag FROM debt_accounts
                ) u ON da.user_id = u.user_id
                WHERE da.is_public = 1
                ORDER BY da.current_balance DESC
            ''')
            
            public_accounts = await cursor.fetchall()
        
        # Group accounts by user
        user_accounts = {}
        for account in public_accounts:
            user_id = account[1]
            if user_id not in user_accounts:
                user_accounts[user_id] = []
            user_accounts[user_id].append(account)
            
        # Add user accounts to embed
        for user_id, accounts in user_accounts.items():
            user = self.get_user(user_id)
            # If we can't get the user object, fetch it (might not be in cache)
            if not user:
                try:
                    user = await self.fetch_user(user_id)
                except discord.NotFound:
                    # If we still can't find the user, show Anonymous User instead of ID
                    user_display_name = "Anonymous User"
                except Exception as e:
                    logger.error(f"Error fetching user {user_id}: {e}")
                    user_display_name = "Anonymous User"
                else:
                    user_display_name = user.display_name
            else:
                user_display_name = user.display_name
            
            account_list = []
            total_current = 0
            total_initial = 0
            
            for account in accounts:
                account_id = account[0]
                name = account[2]
                current_balance = account[3]
                initial_balance = account[4]
                interest_rate = account[5]
                
                total_current += current_balance
                total_initial += initial_balance
                
                # Calculate percentage paid
                paid_percentage = 0
                if initial_balance > 0:
                    paid_percentage = 100 - (current_balance / initial_balance * 100)
                
                # Create progress bar
                progress_bar = self._create_progress_bar(paid_percentage)
                
                # Format interest rate display
                interest_display = f" ({interest_rate}%)" if interest_rate > 0 else ""
                
                account_list.append(
                    f"**{name}**{interest_display} - `${current_balance:,.2f}/${initial_balance:,.2f}`\n"
                    f"{progress_bar} ({paid_percentage:.1f}% paid)"
                )
            
            # Calculate total progress
            total_percentage = 0
            if total_initial > 0:
                total_percentage = 100 - (total_current / total_initial * 100)
                
            total_progress_bar = self._create_progress_bar(total_percentage)
            
            # Add user's accounts to embed
            user_accounts_text = "\n\n".join(account_list)
            
            embed.add_field(
                name=f"👤 {user_display_name}",
                value=f"{user_accounts_text}\n\n" +
                      f"**TOTAL:** `${total_current:,.2f}/${total_initial:,.2f}`\n" +
                      f"{total_progress_bar} ({total_percentage:.1f}% paid)",
                inline=False
            )
            
        if not public_accounts:
            embed.add_field(
                name="No Debt Accounts Yet",
                value="Use `/debt add` to create your first debt account!",
                inline=False
            )
            
        # Add instructions
        embed.add_field(
            name="📋 Commands",
            value=(
                "• `/debt add` - Add a new debt account\n"
                "• `/debt list` - List your debt accounts\n"
                "• `/debt payment` - Record a payment\n"
                "• `/debt edit` - Update an account\n"
                "• `/debt delete` - Remove an account"
            ),
            inline=False
        )
        
        embed.set_footer(text=f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')} • Use the buttons below to update your accounts")
        
        return embed
        
    def _create_progress_bar(self, percentage):
        """Create a visual progress bar based on percentage."""
        if percentage < 0:
            percentage = 0
        elif percentage > 100:
            percentage = 100
            
        filled_blocks = int(percentage / 10)
        empty_blocks = 10 - filled_blocks
        
        return "🟩" * filled_blocks + "⬜" * empty_blocks

    async def _get_recent_messages(self, channel, limit=50):
        """Helper method to get recent messages from a channel in a Discord.py v2.x compatible way."""
        messages = []
        async for message in channel.history(limit=limit):
            messages.append(message)
            if len(messages) >= limit:
                break
        return messages

    def _format_duration_seconds(self, duration_seconds_str):
        """Format a duration string in seconds (e.g., '545s') to a human-readable format."""
        try:
            if not isinstance(duration_seconds_str, str) or not duration_seconds_str.endswith('s'):
                return str(duration_seconds_str)
                
            seconds = int(duration_seconds_str.rstrip('s'))
            minutes = seconds // 60
            seconds_remainder = seconds % 60
            
            if minutes >= 60:
                hours = minutes // 60
                minutes_remainder = minutes % 60
                if minutes_remainder > 0:
                    return f"{hours} hr {minutes_remainder} min"
                else:
                    return f"{hours} hr"
            elif minutes > 0:
                if seconds_remainder > 0 and minutes < 10:
                    # Only show seconds for short durations
                    return f"{minutes} min {seconds_remainder} sec"
                else:
                    return f"{minutes} min"
            else:
                return f"{seconds} sec"
        except (ValueError, TypeError):
            return str(duration_seconds_str)

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


async def load_cogs() -> None:
    """
    The code in this function is executed whenever the bot will start.
    """
    for file in os.listdir(f"./cogs"):
        if file.endswith(".py"):
            extension = file[:-3]
            try:
                await bot.load_extension(f"cogs.{extension}")
                print(f"Loaded cog '{extension}'")
            except Exception as e:
                exception = f"{type(e).__name__}: {e}"
                print(f"Failed to load extension {extension}\n{exception}")


if __name__ == '__main__':
    logger.info('🚀 Starting Gentle Habits Bot...')
    try:
        bot = GentleHabitsBot()
        logger.info('✅ Bot instance created, attempting to connect to Discord...')
        asyncio.run(load_cogs())
        bot.run(config.token, reconnect=True)
    except discord.errors.LoginFailure:
        logger.critical('❌ Invalid Discord token provided. Please check your .env file.')
    except discord.errors.PrivilegedIntentsRequired:
        logger.critical('❌ Required privileged intents are not enabled for this bot. Please check the Discord Developer Portal.')
    except discord.errors.HTTPException as e:
        if e.status == 429:  # Rate limit error
            logger.critical(f'❌ Discord API rate limit reached. Please wait {e.retry_after} seconds before restarting.')
        else:
            logger.critical(f'❌ HTTP Error connecting to Discord: {e}')
    except Exception as e:
        logger.critical(f'❌ Failed to start the bot: {str(e)}')
        logger.critical(traceback.format_exc()) 