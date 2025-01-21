# Gentle Habits Discord Bot

A Discord bot designed for gentle, ADHD-friendly habit tracking and reminders with minimal friction. The bot helps users maintain daily streaks, manage restock reminders, and provides positive reinforcement.

## Features

- **Daily Streak Tracking**: Simple button-based check-in system with optional public/private streaks
- **Dedicated Reminder Channel**: Daily reminders with interactive buttons for easy check-ins
- **Gentle Nudges**: Get reminders about your tasks and upcoming restocks
- **Restock Tracking**: Never run out of important items with timely reminders
- **Positive Reinforcement**: Receive supportive affirmations for completing tasks

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
   - Copy the bot token

4. Set up environment variables:
   - Create a `.env` file in the project root
   - Add your Discord token and reminder channel ID:
     ```
     DISCORD_TOKEN=your_token_here
     REMINDER_CHANNEL_ID=your_channel_id_here
     ```

5. Run the bot:
   ```bash
   python bot.py
   ```

## Features

### Channel-Based Reminders
The bot will send daily reminders to a designated channel at 8 PM with:
- An interactive check-in button
- Current streak information
- Encouraging messages

### Streak Tracking
- Click the ✨ Check In button in the reminder channel
- Get private affirmations and streak updates
- Streaks reset at midnight if you miss a day

### Task Reminders
- `/habit gentle-nudge`: Get a gentle reminder of your tasks and streaks

### Restock Tracking
- `/habit restock-add <item_name> <days_until_refill>`: Add an item to track
- `/habit restock-done <item_name>`: Mark an item as restocked
- Restock reminders appear in the reminder channel

## Design Philosophy

- **ADHD-Friendly Design**: All interactions are designed to be low-friction and gentle
- **Privacy-Focused**: Most responses are ephemeral by default
- **Persistent Storage**: All data is stored in SQLite for reliability
- **Automatic Scheduling**: Daily checks and reminders are handled automatically

## Contributing

Feel free to submit issues and enhancement requests! 