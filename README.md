# Gentle Habits Discord Bot

A Discord bot designed for gentle, ADHD-friendly habit tracking and reminders with minimal friction. The bot helps users maintain daily streaks, manage restock reminders, and provides positive reinforcement through a colorful and encouraging interface.

## Features

- **Daily Streak Tracking**: Simple button-based check-in system with streak board updates every 15 minutes
- **Dedicated Reminder Channel**: Customizable daily reminders with interactive buttons for easy check-ins
- **Gentle Nudges**: Personalized reminders about your tasks and upcoming restocks
- **Restock Tracking**: Never run out of important items with 3-day advance reminders
- **Positive Reinforcement**: Receive supportive affirmations from a curated list when completing tasks
- **Multi-User Support**: Track habits and streaks for multiple users in your server
- **Colorful Interface**: Beautiful colored console output for easy monitoring

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
   - Add your Discord token and reminder channel ID:
     ```
     DISCORD_TOKEN=your_token_here
     REMINDER_CHANNEL_ID=your_channel_id_here
     ```

5. Run the bot:
   ```bash
   python bot.py
   ```

## Commands

### Habit Management
- `/habit add <name> <reminder_time> <expiry_time>`: Create a new habit
- `/habit remove <name>`: Remove an existing habit
- `/habit list`: View all your habits
- `/habit join <name>`: Join an existing habit
- `/habit leave <name>`: Leave a habit you're participating in

### Streak System
- Interactive buttons for daily check-ins
- Automatic streak tracking and updates
- Public streak board updated every 15 minutes
- Private streak notifications

### Restock System
- `/habit restock-add <item_name> <days_between_refills>`: Add an item to track
- `/habit restock-list`: View your restock items
- `/habit restock-remove <item_name>`: Remove a restock item
- `/habit restock-done <item_name>`: Mark an item as restocked

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