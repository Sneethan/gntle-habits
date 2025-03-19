# Gentle Habits Discord Bot

A Discord bot designed for gentle, ADHD-friendly habit tracking and reminders with minimal friction. The bot helps users maintain daily streaks, manage restock reminders, and provides positive reinforcement through a colorful and encouraging interface.

## Features

- **Daily Streak Tracking**: Simple button-based check-in system with streak board updates every 15 minutes
- **Dedicated Reminder Channel**: Customizable daily reminders with interactive buttons for easy check-ins
- **Gentle Nudges**: Personalized reminders about your tasks and upcoming restocks
- **Restock Tracking**: Never run out of important items with 3-day advance reminders
- **Positive Reinforcement**: Receive supportive affirmations from a curated list when completing tasks
- **Multi-User Support**: Track habits and streaks for multiple users in your server
- **Morning Briefings**: Daily personalized briefings with weather, transit, and traffic information
- **Google Maps Integration**: Real-time traffic analysis and transit information with deep links to Google Maps
- **Colorful Interface**: Beautiful colored console output for easy monitoring
- **Timezone Support**: Configurable timezone for all reminders and schedules
- **Reliability Features**: Automatic catch-up for missed reminders after restarts
- **Configurable Affirmation Tone**: Style of encouragement messages (gentle, balanced, or firm)

## Requirements

- Python 3.8 or higher
- Discord.py 2.0 or higher
- Required Discord Bot Permissions:
  - Send Messages
  - Embed Links
  - Add Reactions
  - Use External Emojis
  - Manage Messages
  - View Channels

## Setup

1. Install Python 3.8 or higher
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Create a Discord bot and get your token:
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - Create a New Application
   - Go to the Bot section and create a bot
   - Enable necessary Privileged Gateway Intents:
     - Message Content Intent
   - Copy the bot token

4. Set up environment variables:
   - Create a `.env` file in the project root (use `.env.example` as a template)
   - Add your Discord token, reminder channel ID, and timezone:
     ```
     DISCORD_TOKEN=your_token_here
     REMINDER_CHANNEL_ID=your_channel_id_here
     TIMEZONE=your_timezone_here  # e.g., America/New_York
     AFFIRMATION_TONE=balanced
     GOOGLE_MAPS_API_KEY=your_google_maps_api_key_here
     ```

5. Run the bot:
   ```bash
   python bot.py
   ```

## Configuration

### Required Environment Variables
- `DISCORD_TOKEN`: Your Discord bot token
- `REMINDER_CHANNEL_ID`: Channel ID for reminders and streak board
- `DEEPSEEK_API_KEY`: API key for task breakdown feature
- `GOOGLE_MAPS_API_KEY`: Google Maps API key for transit and traffic information
  - Required API services: Routes API and Places API
  - The Routes API has replaced the legacy Directions API
  - Enable these in the Google Cloud Console before using
  - Make sure billing is set up for your Google Cloud project
- `TIMEZONE`: IANA timezone name (defaults to UTC)
- `AFFIRMATION_TONE`: Style of encouragement messages (defaults to balanced)
  - `gentle`: Soft, nurturing encouragement
  - `balanced`: Standard positive reinforcement
  - `firm`: More assertive, motivational tone

### Optional Environment Variables
- `DB_PATH`: Database file path (defaults to gentle_habits.db)
- `MAX_DB_CONNECTIONS`: Maximum database connections (defaults to 5)
- `STREAK_UPDATE_INTERVAL`: Minutes between streak updates (defaults to 5)
- `LOG_LEVEL`: Logging level (defaults to INFO)

## Reliability Features

### Timezone Handling
- All times are stored in a consistent format
- Automatic conversion between UTC and local timezone
- Proper handling of daylight saving time transitions
- Configurable through environment variables

### Restart Recovery
- Checks for missed reminders from the last hour on startup
- Only sends catch-up reminders for non-expired tasks
- Maintains streak consistency across restarts
- Logs all catch-up actions for monitoring

## Commands

### Habit Management
- `/habit create` - Create a new habit to track
- `/habit list` - View all your habits
- `/habit edit` - Modify an existing habit
- `/habit delete` - Remove a habit

### Streak System
- Automatic streak tracking and updates
- Public streak board updated every 15 minutes
- Private streak notifications

### Restock System
- `/habit restock-add <item_name> <days_between_refills>`: Add an item to track
- `/habit restock-list`: View your restock items
- `/habit restock-remove <item_name>`: Remove a restock item
- `/habit restock-done <item_name>`: Mark an item as restocked

### Morning Briefing System
- `/briefing opt-in <time> <location>`: Subscribe to daily briefings at specified time
- `/briefing opt-out`: Unsubscribe from daily briefings
- `/briefing set-time <time>`: Update briefing delivery time
- `/briefing set-location <location>`: Update your location for weather information
- `/briefing set-bus-origin <nickname> <address>`: Set your transit journey starting point
- `/briefing set-bus-destination <nickname> <address>`: Set your transit journey destination
- `/briefing status`: Check your current briefing settings
- `/briefing test`: Send a test briefing to check your settings
- `/briefing countdown-add <event> <date>`: Add event countdowns to your briefings

## Morning Briefings

The Morning Briefing system provides personalized daily information including:

- **Weather Forecasts**: Current conditions and daily forecast for your location
- **Transit Information**: Bus schedule information using Google Maps Routes API (the modern replacement for Directions API)
- **Traffic Analysis**: Real-time traffic conditions for driving between your set locations
- **Google Maps Deep Links**: Open your route directly in Google Maps on your phone
- **Restock Reminders**: Notifications about items that need restocking soon
- **Event Countdowns**: Countdown to upcoming important events

Briefings are sent via direct message at your specified time each day, and can be customized to include only the information you need.

## Design Philosophy

- **ADHD-Friendly Design**: All interactions are designed to be low-friction and gentle
- **Privacy-Focused**: Most responses are ephemeral by default
- **Persistent Storage**: All data is stored in SQLite for reliability
- **Automatic Scheduling**: Daily checks and reminders are handled automatically
- **Positive Reinforcement**: Encouraging messages and streak tracking to build motivation

## Database Structure

The bot uses SQLite with the following main tables:
- `habits`: Stores habit definitions and schedules
- `user_habits`: Tracks individual user progress and streaks
- `habit_participants`: Manages user participation in habits
- `restock_items`: Tracks items that need periodic restocking
- `affirmations`: Stores encouraging messages for positive reinforcement

## Contributing

Feel free to submit issues and enhancement requests! Pull requests are welcome.

## License

This project is open source and available under the MIT License. 