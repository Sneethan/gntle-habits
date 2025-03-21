import discord
from datetime import datetime
import aiosqlite
import random
import json
import logging
from assets.utils.utils import get_current_time, convert_to_local, convert_to_utc

# Just get the logger without adding handlers
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
        super().__init__(timeout=None)
        self.add_item(StreakButton())
        
class DebtTrackerView(discord.ui.View):
    """View for the debt tracker dashboard with interactive buttons."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(AddDebtButton())
        self.add_item(MakePaymentButton())
        self.add_item(UpdateBalanceButton())
        self.add_item(RefreshDashboardButton())

class AddDebtButton(discord.ui.Button):
    """Button to quickly add a new debt account."""
    def __init__(self):
        super().__init__(
            label="Add Debt Account",
            style=discord.ButtonStyle.green,
            custom_id="add_debt_account",
            emoji="💰"
        )
        
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddDebtModal())

class MakePaymentButton(discord.ui.Button):
    """Button to record a payment."""
    def __init__(self):
        super().__init__(
            label="Record Payment",
            style=discord.ButtonStyle.blurple,
            custom_id="make_debt_payment",
            emoji="💸"
        )
        
    async def callback(self, interaction: discord.Interaction):
        # Get user's accounts for the dropdown
        async with aiosqlite.connect(interaction.client.db_path) as db:
            cursor = await db.execute(
                'SELECT id, name FROM debt_accounts WHERE user_id = ? ORDER BY name',
                (interaction.user.id,)
            )
            accounts = await cursor.fetchall()
            
        if not accounts:
            await interaction.response.send_message(
                "You don't have any debt accounts yet! Use the Add Debt Account button to create one first.",
                ephemeral=True
            )
            return
        
        # We can only show a modal directly
        modal = MakePaymentModal(accounts)
        await interaction.response.send_modal(modal)

class UpdateBalanceButton(discord.ui.Button):
    """Button to update a debt balance."""
    def __init__(self):
        super().__init__(
            label="Update Balance",
            style=discord.ButtonStyle.grey,
            custom_id="update_debt_balance",
            emoji="🔄"
        )
        
    async def callback(self, interaction: discord.Interaction):
        # Get user's accounts for the dropdown
        async with aiosqlite.connect(interaction.client.db_path) as db:
            cursor = await db.execute(
                'SELECT id, name FROM debt_accounts WHERE user_id = ? ORDER BY name',
                (interaction.user.id,)
            )
            accounts = await cursor.fetchall()
            
        if not accounts:
            await interaction.response.send_message(
                "You don't have any debt accounts yet! Use the Add Debt Account button to create one first.",
                ephemeral=True
            )
            return
            
        await interaction.response.send_modal(UpdateBalanceModal(accounts))

class RefreshDashboardButton(discord.ui.Button):
    """Button to refresh the debt dashboard."""
    def __init__(self):
        super().__init__(
            label="Refresh Dashboard",
            style=discord.ButtonStyle.grey,
            custom_id="refresh_debt_dashboard",
            emoji="🔄"
        )
        
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.client.update_debt_dashboard()
        await interaction.followup.send("Debt tracker dashboard refreshed!", ephemeral=True)

class AddDebtModal(discord.ui.Modal, title="Add New Debt Account"):
    """Modal for adding a new debt account."""
    name = discord.ui.TextInput(
        label="Account Name",
        placeholder="Credit Card, Student Loan, etc.",
        required=True,
        max_length=100
    )
    
    current_balance = discord.ui.TextInput(
        label="Current Balance ($)",
        placeholder="1000.00",
        required=True
    )
    
    interest_rate = discord.ui.TextInput(
        label="Interest Rate (%)",
        placeholder="Optional - e.g. 19.99",
        required=False
    )
    
    due_date = discord.ui.TextInput(
        label="Due Date (YYYY-MM-DD)",
        placeholder="Optional - next payment due date",
        required=False
    )
    
    is_public = discord.ui.TextInput(
        label="Public? (yes/no)",
        placeholder="yes",
        default="yes",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Validate current balance as a number
            try:
                current_balance = float(self.current_balance.value)
                if current_balance < 0:
                    raise ValueError("Balance must be a positive number")
            except ValueError:
                await interaction.response.send_message(
                    "Please enter a valid number for the current balance.",
                    ephemeral=True
                )
                return
                
            # Validate interest rate if provided
            interest_rate = 0.0
            if self.interest_rate.value:
                try:
                    interest_rate = float(self.interest_rate.value)
                    if interest_rate < 0:
                        raise ValueError("Interest rate cannot be negative")
                except ValueError:
                    await interaction.response.send_message(
                        "Please enter a valid number for the interest rate.",
                        ephemeral=True
                    )
                    return
                    
            # Validate due date if provided
            due_date = None
            if self.due_date.value:
                try:
                    due_date = datetime.strptime(self.due_date.value, "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    await interaction.response.send_message(
                        "Please enter the due date in YYYY-MM-DD format.",
                        ephemeral=True
                    )
                    return
                    
            # Validate is_public
            is_public = self.is_public.value.lower() in ["yes", "y", "true"]
            
            # Insert into database
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with aiosqlite.connect(interaction.client.db_path) as db:
                try:
                    await db.execute(
                        '''
                        INSERT INTO debt_accounts (
                            user_id, name, current_balance, initial_balance, 
                            interest_rate, due_date, created_at, updated_at, is_public
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            interaction.user.id,
                            self.name.value,
                            current_balance,
                            current_balance,  # Initial balance is the starting balance
                            interest_rate,
                            due_date,
                            now,
                            now,
                            is_public
                        )
                    )
                    await db.commit()
                except aiosqlite.IntegrityError:
                    await interaction.response.send_message(
                        f"You already have a debt account named '{self.name.value}'.",
                        ephemeral=True
                    )
                    return
            
            await interaction.response.send_message(
                f"Debt account '{self.name.value}' added successfully with a balance of ${current_balance:,.2f}!",
                ephemeral=True
            )
            
            # Refresh the dashboard
            await interaction.client.update_debt_dashboard()
            
        except Exception as e:
            logger.error(f"Error adding debt account: {e}")
            await interaction.response.send_message(
                "An error occurred while adding your debt account. Please try again.",
                ephemeral=True
            )

class MakePaymentModal(discord.ui.Modal, title="Record a Payment"):
    """Modal for recording a payment on a debt account."""
    def __init__(self, accounts):
        super().__init__()
        
        # Store accounts for reference in on_submit
        self.accounts = accounts
        
        # Create account info for the placeholder
        account_list = ", ".join([f"{account_id}: {name}" for account_id, name in accounts])
        
        self.account_id = discord.ui.TextInput(
            label="Account ID",
            placeholder=f"Available accounts: {account_list}",
            required=True,
            custom_id="account_id"
        )
        
        self.payment_amount = discord.ui.TextInput(
            label="Payment Amount ($)",
            placeholder="100.00",
            required=True,
            custom_id="payment_amount"
        )
        
        self.payment_date = discord.ui.TextInput(
            label="Date (YYYY-MM-DD, leave blank for today)",
            placeholder="YYYY-MM-DD",
            required=False,
            custom_id="payment_date"
        )
        
        self.notes = discord.ui.TextInput(
            label="Notes (Optional)",
            placeholder="Extra payment from bonus",
            required=False,
            custom_id="notes"
        )
        
        # Add all items
        self.add_item(self.account_id)
        self.add_item(self.payment_amount)
        self.add_item(self.payment_date)  
        self.add_item(self.notes)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Get values from inputs
            selected_account_id = int(self.account_id.value)
            account_name = next((name for account_id, name in self.accounts if account_id == selected_account_id), "Unknown")
            
            # Get text input values
            payment_amount = self.payment_amount.value
            payment_date = self.payment_date.value
            notes = self.notes.value
            
            # Validate payment amount
            try:
                payment_amount = float(payment_amount)
                if payment_amount <= 0:
                    raise ValueError("Payment amount must be positive")
            except ValueError:
                await interaction.response.send_message(
                    "Please enter a valid positive number for the payment amount.",
                    ephemeral=True
                )
                return
                
            # Validate and set payment date
            if not payment_date:
                payment_date = datetime.now().strftime("%Y-%m-%d")
            else:
                try:
                    payment_date = datetime.strptime(payment_date, "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    await interaction.response.send_message(
                        "Please enter the payment date in YYYY-MM-DD format.",
                        ephemeral=True
                    )
                    return
            
            # Update the database
            async with aiosqlite.connect(interaction.client.db_path) as db:
                await db.execute('BEGIN TRANSACTION')
                try:
                    # Get current balance
                    cursor = await db.execute(
                        'SELECT current_balance FROM debt_accounts WHERE id = ?',
                        (selected_account_id,)
                    )
                    result = await cursor.fetchone()
                    if not result:
                        await db.rollback()
                        await interaction.response.send_message(
                            "Could not find the selected account.",
                            ephemeral=True
                        )
                        return
                        
                    current_balance = result[0]
                    new_balance = current_balance - payment_amount
                    
                    if new_balance < 0:
                        new_balance = 0  # Prevent negative balances
                    
                    # Record the payment
                    await db.execute(
                        'INSERT INTO debt_payments (account_id, amount, payment_date, description) VALUES (?, ?, ?, ?)',
                        (selected_account_id, payment_amount, payment_date, notes)
                    )
                    
                    # Update the account balance
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    await db.execute(
                        'UPDATE debt_accounts SET current_balance = ?, updated_at = ? WHERE id = ?',
                        (new_balance, now, selected_account_id)
                    )
                    
                    await db.commit()
                    
                    await interaction.response.send_message(
                        f"Payment of ${payment_amount:,.2f} recorded for '{account_name}'! "
                        f"New balance: ${new_balance:,.2f}",
                        ephemeral=True
                    )
                    
                    # Refresh the dashboard
                    await interaction.client.update_debt_dashboard()
                    
                except Exception as e:
                    await db.rollback()
                    logger.error(f"Error recording payment: {e}")
                    await interaction.response.send_message(
                        "An error occurred while recording your payment. Please try again.",
                        ephemeral=True
                    )
        except Exception as e:
            logger.error(f"Error in payment modal: {e}")
            await interaction.response.send_message(
                "An error occurred while processing your payment. Please try again.",
                ephemeral=True
            )

class UpdateBalanceModal(discord.ui.Modal, title="Update Debt Balance"):
    """Modal for updating a debt balance directly."""
    def __init__(self, accounts):
        super().__init__()
        
        # Store accounts for reference in on_submit
        self.accounts = accounts
        
        # Create account info for the placeholder
        account_list = ", ".join([f"{account_id}: {name}" for account_id, name in accounts])
        
        self.account_id = discord.ui.TextInput(
            label="Account ID",
            placeholder=f"Available accounts: {account_list}",
            required=True,
            custom_id="account_id"
        )
        
        self.new_balance = discord.ui.TextInput(
            label="New Balance ($)",
            placeholder="Current balance amount",
            required=True,
            custom_id="new_balance"
        )
        
        self.reason = discord.ui.TextInput(
            label="Reason for Update (Optional)",
            placeholder="Interest applied, error correction, etc.",
            required=False,
            custom_id="reason"
        )
        
        # Add all items
        self.add_item(self.account_id)
        self.add_item(self.new_balance)
        self.add_item(self.reason)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Get values from inputs
            selected_account_id = int(self.account_id.value)
            account_name = next((name for account_id, name in self.accounts if account_id == selected_account_id), "Unknown")
            
            # Get text input values
            new_balance = self.new_balance.value
            reason = self.reason.value
            
            # Validate new balance
            try:
                new_balance = float(new_balance)
                if new_balance < 0:
                    raise ValueError("Balance cannot be negative")
            except ValueError:
                await interaction.response.send_message(
                    "Please enter a valid non-negative number for the balance.",
                    ephemeral=True
                )
                return
            
            # Update the database
            async with aiosqlite.connect(interaction.client.db_path) as db:
                # Get current balance
                cursor = await db.execute(
                    'SELECT current_balance FROM debt_accounts WHERE id = ?',
                    (selected_account_id,)
                )
                result = await cursor.fetchone()
                if not result:
                    await interaction.response.send_message(
                        "Could not find the selected account.",
                        ephemeral=True
                    )
                    return
                    
                current_balance = result[0]
                
                # Record note about the update if there's a reason
                if reason:
                    now = datetime.now().strftime("%Y-%m-%d")
                    balance_diff = new_balance - current_balance
                    sign = "+" if balance_diff >= 0 else ""
                    
                    note = f"Balance adjusted by {sign}${abs(balance_diff):,.2f}: {reason}"
                    
                    await db.execute(
                        'INSERT INTO debt_payments (account_id, amount, payment_date, description) VALUES (?, ?, ?, ?)',
                        (selected_account_id, -balance_diff, now, note)  # Negative amount because this isn't a payment
                    )
                
                # Update the account balance
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                await db.execute(
                    'UPDATE debt_accounts SET current_balance = ?, updated_at = ? WHERE id = ?',
                    (new_balance, now, selected_account_id)
                )
                
                await db.commit()
                
                await interaction.response.send_message(
                    f"Balance for '{account_name}' updated to ${new_balance:,.2f}!",
                    ephemeral=True
                )
                
                # Refresh the dashboard
                await interaction.client.update_debt_dashboard()
                
        except Exception as e:
            logger.error(f"Error updating balance: {e}")
            await interaction.response.send_message(
                "An error occurred while updating your balance. Please try again.",
                ephemeral=True
            ) 