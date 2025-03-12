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
    bot.tree.add_command(BriefingCommands(bot))