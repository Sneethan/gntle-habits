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

class DebtCommands(app_commands.Group):
    """Commands for tracking and managing debt accounts."""
    
    def __init__(self, bot):
        super().__init__(name="debt", description="Track and manage debt accounts")
        self.bot = bot
    
    @app_commands.command(name="add", description="Add a new debt account to track")
    @app_commands.describe(
        name="Name of the debt account (e.g., 'Credit Card', 'Student Loan')",
        balance="Current balance on the account",
        interest_rate="Annual interest rate as a percentage (optional)",
        due_date="Next payment due date (YYYY-MM-DD format, optional)",
        description="Optional notes about this debt",
        is_public="Whether to show this debt on the public dashboard"
    )
    async def add_debt(
        self,
        interaction: discord.Interaction,
        name: str,
        balance: float,
        interest_rate: float = None,
        due_date: str = None,
        description: str = None,
        is_public: bool = True
    ):
        # Validate inputs
        if balance < 0:
            await interaction.response.send_message(
                "Balance must be a positive number.",
                ephemeral=True
            )
            return
        
        if interest_rate is not None and interest_rate < 0:
            await interaction.response.send_message(
                "Interest rate cannot be negative.",
                ephemeral=True
            )
            return
        
        # Validate due date format if provided
        if due_date:
            try:
                due_date = datetime.strptime(due_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                await interaction.response.send_message(
                    "Due date must be in YYYY-MM-DD format.",
                    ephemeral=True
                )
                return
        
        try:
            # Insert into database
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with aiosqlite.connect(self.bot.db_path) as db:
                try:
                    await db.execute(
                        '''
                        INSERT INTO debt_accounts (
                            user_id, name, current_balance, initial_balance, 
                            interest_rate, due_date, description, created_at, updated_at, is_public
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            interaction.user.id,
                            name,
                            balance,
                            balance,  # Initial balance is the starting balance
                            interest_rate if interest_rate is not None else 0.0,
                            due_date,
                            description,
                            now,
                            now,
                            is_public
                        )
                    )
                    await db.commit()
                except aiosqlite.IntegrityError:
                    await interaction.response.send_message(
                        f"You already have a debt account named '{name}'.",
                        ephemeral=True
                    )
                    return
            
            await interaction.response.send_message(
                f"Debt account '{name}' added successfully with a balance of ${balance:,.2f}!",
                ephemeral=True
            )
            
            # Update the debt dashboard
            await self.bot.update_debt_dashboard()
            
        except Exception as e:
            logging.error(f"Error adding debt account: {e}")
            await interaction.response.send_message(
                "An error occurred while adding your debt account. Please try again.",
                ephemeral=True
            )
    
    async def account_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for user's debt account names."""
        try:
            # Print debug info
            print(f"Autocomplete called for user {interaction.user.id} with current: '{current}'")
            
            # Get all debt accounts for this user
            async with aiosqlite.connect(self.bot.db_path) as db:
                # First check if the user has any accounts
                cursor = await db.execute(
                    'SELECT COUNT(*) FROM debt_accounts WHERE user_id = ?',
                    (interaction.user.id,)
                )
                count = await cursor.fetchone()
                print(f"User has {count[0]} debt accounts")
                
                if count and count[0] == 0:
                    # User has no accounts, return empty list
                    return []
                
                # Get the accounts
                cursor = await db.execute(
                    'SELECT name FROM debt_accounts WHERE user_id = ? AND name LIKE ?',
                    (interaction.user.id, f"%{current}%")
                )
                accounts = await cursor.fetchall()
                print(f"Found {len(accounts)} matching accounts for user")
            
            # Create choices
            choices = []
            for account in accounts:
                name = account[0]
                choices.append(app_commands.Choice(name=name, value=name))
                if len(choices) >= 25:  # Discord limit
                    break
            
            print(f"Returning {len(choices)} choices for autocomplete")
            return choices
            
        except Exception as e:
            print(f"Error in debt account autocomplete: {str(e)}")
            import traceback
            traceback.print_exc()
            return []
    
    @app_commands.command(name="list", description="List all your debt accounts and balances")
    @app_commands.describe(
        show_private="Whether to include private accounts in the list"
    )
    async def list_debts(self, interaction: discord.Interaction, show_private: bool = True):
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                # Query to get all user's accounts
                query = '''
                    SELECT id, name, current_balance, initial_balance, interest_rate, 
                           due_date, description, is_public
                    FROM debt_accounts 
                    WHERE user_id = ?
                '''
                
                if not show_private:
                    query += ' AND is_public = 1'
                    
                query += ' ORDER BY current_balance DESC'
                
                cursor = await db.execute(query, (interaction.user.id,))
                accounts = await cursor.fetchall()
                
                if not accounts:
                    await interaction.response.send_message(
                        "You don't have any debt accounts yet! Use `/debt add` to create one.",
                        ephemeral=True
                    )
                    return
                
                # Create embed to display accounts
                embed = discord.Embed(
                    title="Your Debt Tracker Accounts",
                    description="Here's a summary of all your debt accounts",
                    color=discord.Color.teal()
                )
                
                total_current = 0
                total_initial = 0
                
                for account in accounts:
                    account_id, name, current, initial, rate, due_date, desc, is_public = account
                    
                    total_current += current
                    total_initial += initial
                    
                    # Calculate percentage paid
                    paid_percentage = 0
                    if initial > 0:
                        paid_percentage = 100 - (current / initial * 100)
                    
                    # Create progress bar
                    progress_bar = self.bot._create_progress_bar(paid_percentage)
                    
                    # Format interest rate display
                    interest_display = f" ({rate}%)" if rate > 0 else ""
                    
                    # Format due date display
                    due_display = f"\nDue: {due_date}" if due_date else ""
                    
                    # Format description
                    desc_display = f"\n{desc}" if desc else ""
                    
                    # Privacy indicator
                    privacy = "🔒 Private" if not is_public else "🌐 Public"
                    
                    embed.add_field(
                        name=f"{name}{interest_display} ({privacy})",
                        value=(
                            f"Balance: `${current:,.2f}/${initial:,.2f}`\n"
                            f"{progress_bar} ({paid_percentage:.1f}% paid)"
                            f"{due_display}{desc_display}"
                        ),
                        inline=False
                    )
                
                # Calculate total progress
                total_percentage = 0
                if total_initial > 0:
                    total_percentage = 100 - (total_current / total_initial * 100)
                    
                total_progress_bar = self.bot._create_progress_bar(total_percentage)
                
                embed.add_field(
                    name="📊 OVERALL PROGRESS",
                    value=(
                        f"Total Debt: `${total_current:,.2f}/${total_initial:,.2f}`\n"
                        f"{total_progress_bar} ({total_percentage:.1f}% paid)"
                    ),
                    inline=False
                )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
        except Exception as e:
            logging.error(f"Error listing debt accounts: {e}")
            await interaction.response.send_message(
                "An error occurred while retrieving your debt accounts. Please try again.",
                ephemeral=True
            )
    
    @app_commands.command(name="payment", description="Record a payment on a debt account")
    @app_commands.describe(
        account_name="Name of the debt account",
        amount="Payment amount",
        date="Payment date (YYYY-MM-DD format, defaults to today)",
        notes="Optional notes about this payment"
    )
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    async def record_payment(
        self,
        interaction: discord.Interaction,
        account_name: str,
        amount: float,
        date: str = None,
        notes: str = None
    ):
        # Validate inputs
        if amount <= 0:
            await interaction.response.send_message(
                "Payment amount must be positive.",
                ephemeral=True
            )
            return
        
        # Validate date format if provided
        payment_date = datetime.now().strftime("%Y-%m-%d")
        if date:
            try:
                payment_date = datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                await interaction.response.send_message(
                    "Date must be in YYYY-MM-DD format.",
                    ephemeral=True
                )
                return
        
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                await db.execute('BEGIN TRANSACTION')
                try:
                    # Get account info
                    cursor = await db.execute(
                        'SELECT id, current_balance FROM debt_accounts WHERE user_id = ? AND name = ?',
                        (interaction.user.id, account_name)
                    )
                    account = await cursor.fetchone()
                    
                    if not account:
                        await db.rollback()
                        await interaction.response.send_message(
                            f"You don't have a debt account named '{account_name}'.",
                            ephemeral=True
                        )
                        return
                        
                    account_id, current_balance = account
                    
                    # Calculate new balance
                    new_balance = current_balance - amount
                    if new_balance < 0:
                        new_balance = 0  # Don't allow negative balances
                    
                    # Record the payment
                    await db.execute(
                        'INSERT INTO debt_payments (account_id, amount, payment_date, description) VALUES (?, ?, ?, ?)',
                        (account_id, amount, payment_date, notes)
                    )
                    
                    # Update the account balance
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    await db.execute(
                        'UPDATE debt_accounts SET current_balance = ?, updated_at = ? WHERE id = ?',
                        (new_balance, now, account_id)
                    )
                    
                    await db.commit()
                    
                    await interaction.response.send_message(
                        f"Payment of ${amount:,.2f} recorded for '{account_name}'! New balance: ${new_balance:,.2f}",
                        ephemeral=True
                    )
                    
                    # Update the debt dashboard
                    await self.bot.update_debt_dashboard()
                    
                except Exception as e:
                    await db.rollback()
                    raise e
                
        except Exception as e:
            logging.error(f"Error recording payment: {e}")
            await interaction.response.send_message(
                "An error occurred while recording your payment. Please try again.",
                ephemeral=True
            )
    
    @app_commands.command(name="edit", description="Edit an existing debt account")
    @app_commands.describe(
        account_name="Name of the debt account to edit",
        new_name="New name for the account (optional)",
        new_balance="New current balance (optional)",
        interest_rate="New interest rate (optional)",
        due_date="New due date (YYYY-MM-DD format, optional)",
        description="New description (optional)",
        is_public="Whether to show this debt on the public dashboard (optional)"
    )
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    async def edit_debt(
        self,
        interaction: discord.Interaction,
        account_name: str,
        new_name: str = None,
        new_balance: float = None,
        interest_rate: float = None,
        due_date: str = None,
        description: str = None,
        is_public: bool = None
    ):
        # Make sure at least one field is being updated
        if all(param is None for param in [new_name, new_balance, interest_rate, due_date, description, is_public]):
            await interaction.response.send_message(
                "You need to provide at least one field to update.",
                ephemeral=True
            )
            return
        
        # Validate inputs
        if new_balance is not None and new_balance < 0:
            await interaction.response.send_message(
                "Balance must be a positive number.",
                ephemeral=True
            )
            return
        
        if interest_rate is not None and interest_rate < 0:
            await interaction.response.send_message(
                "Interest rate cannot be negative.",
                ephemeral=True
            )
            return
        
        # Validate due date format if provided
        if due_date:
            try:
                due_date = datetime.strptime(due_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                await interaction.response.send_message(
                    "Due date must be in YYYY-MM-DD format.",
                    ephemeral=True
                )
                return
        
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                await db.execute('BEGIN TRANSACTION')
                try:
                    # Check if account exists
                    cursor = await db.execute(
                        'SELECT id, current_balance FROM debt_accounts WHERE user_id = ? AND name = ?',
                        (interaction.user.id, account_name)
                    )
                    account = await cursor.fetchone()
                    
                    if not account:
                        await db.rollback()
                        await interaction.response.send_message(
                            f"You don't have a debt account named '{account_name}'.",
                            ephemeral=True
                        )
                        return
                        
                    account_id, current_balance = account
                    
                    # Prepare update query
                    update_fields = []
                    update_values = []
                    
                    if new_name is not None:
                        update_fields.append("name = ?")
                        update_values.append(new_name)
                    
                    if new_balance is not None:
                        update_fields.append("current_balance = ?")
                        update_values.append(new_balance)
                        
                        # Record the balance change
                        balance_diff = new_balance - current_balance
                        if balance_diff != 0:
                            sign = "+" if balance_diff > 0 else ""
                            note = f"Balance manually adjusted by {sign}${abs(balance_diff):,.2f}"
                            
                            now = datetime.now().strftime("%Y-%m-%d")
                            await db.execute(
                                'INSERT INTO debt_payments (account_id, amount, payment_date, description) VALUES (?, ?, ?, ?)',
                                (account_id, -balance_diff, now, note)  # Negative amount because this is an adjustment
                            )
                    
                    if interest_rate is not None:
                        update_fields.append("interest_rate = ?")
                        update_values.append(interest_rate)
                    
                    if due_date is not None:
                        update_fields.append("due_date = ?")
                        update_values.append(due_date)
                    
                    if description is not None:
                        update_fields.append("description = ?")
                        update_values.append(description)
                    
                    if is_public is not None:
                        update_fields.append("is_public = ?")
                        update_values.append(is_public)
                    
                    # Add updated_at field
                    update_fields.append("updated_at = ?")
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    update_values.append(now)
                    
                    # Build and execute the update query
                    if update_fields:
                        query = f"UPDATE debt_accounts SET {', '.join(update_fields)} WHERE id = ?"
                        update_values.append(account_id)
                        await db.execute(query, update_values)
                        
                    await db.commit()
                    
                    # Prepare success message
                    updated_fields = []
                    if new_name is not None:
                        updated_fields.append(f"name to '{new_name}'")
                    if new_balance is not None:
                        updated_fields.append(f"balance to ${new_balance:,.2f}")
                    if interest_rate is not None:
                        updated_fields.append(f"interest rate to {interest_rate}%")
                    if due_date is not None:
                        updated_fields.append(f"due date to {due_date}")
                    if description is not None:
                        updated_fields.append("description")
                    if is_public is not None:
                        updated_fields.append(f"visibility to {'public' if is_public else 'private'}")
                    
                    message = f"Updated {account_name}: " + ", ".join(updated_fields)
                    
                    await interaction.response.send_message(message, ephemeral=True)
                    
                    # Update the debt dashboard
                    await self.bot.update_debt_dashboard()
                    
                except aiosqlite.IntegrityError:
                    await db.rollback()
                    await interaction.response.send_message(
                        f"You already have a debt account with the name '{new_name}'.",
                        ephemeral=True
                    )
                    return
                except Exception as e:
                    await db.rollback()
                    raise e
                
        except Exception as e:
            logging.error(f"Error editing debt account: {e}")
            await interaction.response.send_message(
                "An error occurred while updating your debt account. Please try again.",
                ephemeral=True
            )
    
    @app_commands.command(name="delete", description="Delete a debt account")
    @app_commands.describe(
        account_name="Name of the debt account to delete"
    )
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    async def delete_debt(
        self,
        interaction: discord.Interaction,
        account_name: str,
    ):
        """Delete a debt account and all associated payments."""
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                await db.execute('BEGIN TRANSACTION')
                try:
                    # Check if account exists
                    cursor = await db.execute(
                        'SELECT id FROM debt_accounts WHERE user_id = ? AND name = ?',
                        (interaction.user.id, account_name)
                    )
                    account = await cursor.fetchone()
                    
                    if not account:
                        await db.rollback()
                        await interaction.response.send_message(
                            f"You don't have a debt account named '{account_name}'.",
                            ephemeral=True
                        )
                        return
                    
                    account_id = account[0]
                    
                    # Delete associated payments
                    await db.execute(
                        'DELETE FROM debt_payments WHERE account_id = ?',
                        (account_id,)
                    )
                    
                    # Delete the account
                    await db.execute(
                        'DELETE FROM debt_accounts WHERE id = ?',
                        (account_id,)
                    )
                    
                    await db.commit()
                    
                    await interaction.response.send_message(
                        f"Debt account '{account_name}' and all its payment history have been deleted.",
                        ephemeral=True
                    )
                    
                    # Update the debt dashboard
                    await self.bot.update_debt_dashboard()
                    
                except Exception as e:
                    await db.rollback()
                    raise e
                
        except Exception as e:
            logging.error(f"Error deleting debt account: {e}")
            await interaction.response.send_message(
                "An error occurred while deleting your debt account. Please try again.",
                ephemeral=True
            )
            
    @app_commands.command(name="history", description="View payment history for a debt account")
    @app_commands.describe(
        account_name="Name of the debt account to view history for"
    )
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    async def payment_history(
        self,
        interaction: discord.Interaction,
        account_name: str
    ):
        try:
            async with aiosqlite.connect(self.bot.db_path) as db:
                # Check if account exists
                cursor = await db.execute(
                    'SELECT id, current_balance, initial_balance FROM debt_accounts WHERE user_id = ? AND name = ?',
                    (interaction.user.id, account_name)
                )
                account = await cursor.fetchone()
                
                if not account:
                    await interaction.response.send_message(
                        f"You don't have a debt account named '{account_name}'.",
                        ephemeral=True
                    )
                    return
                
                account_id, current_balance, initial_balance = account
                
                # Get payment history
                cursor = await db.execute(
                    '''
                    SELECT amount, payment_date, description
                    FROM debt_payments
                    WHERE account_id = ?
                    ORDER BY payment_date DESC
                    LIMIT 15
                    ''',
                    (account_id,)
                )
                payments = await cursor.fetchall()
                
                # Create embed for payment history
                embed = discord.Embed(
                    title=f"Payment History: {account_name}",
                    description=f"Current Balance: ${current_balance:,.2f}\nInitial Balance: ${initial_balance:,.2f}",
                    color=discord.Color.blue()
                )
                
                if not payments:
                    embed.add_field(
                        name="No Payment History",
                        value="No payments have been recorded for this account yet.",
                        inline=False
                    )
                else:
                    for amount, date, description in payments:
                        # Format based on whether it's a payment or adjustment
                        if amount > 0:
                            title = f"💸 Payment: ${amount:,.2f}"
                        else:
                            title = f"🔄 Adjustment: ${-amount:,.2f}"
                        
                        value = f"Date: {date}"
                        if description:
                            value += f"\nNotes: {description}"
                            
                        embed.add_field(
                            name=title,
                            value=value,
                            inline=False
                        )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
        except Exception as e:
            logging.error(f"Error getting payment history: {e}")
            await interaction.response.send_message(
                "An error occurred while retrieving payment history. Please try again.",
                ephemeral=True
            )
    
    @app_commands.command(name="charge", description="Add a charge or fee to increase a debt account balance")
    @app_commands.autocomplete(account_name=account_name_autocomplete)
    @app_commands.describe(
        account_name="Name of the debt account",
        amount="Amount of the charge/fee to add to the debt",
        description="Optional description of what this charge is for"
    )
    async def add_charge(
        self, 
        interaction: discord.Interaction, 
        account_name: str, 
        amount: float, 
        description: str = None
    ):
        """Add a charge or fee to a debt account, increasing its balance."""
        try:
            # Validate inputs
            if amount <= 0:
                await interaction.response.send_message(
                    "The charge amount must be greater than zero.",
                    ephemeral=True
                )
                return
                
            # Find the debt account
            async with aiosqlite.connect(interaction.client.db_path) as db:
                cursor = await db.execute(
                    'SELECT id, current_balance FROM debt_accounts WHERE user_id = ? AND name = ?',
                    (interaction.user.id, account_name)
                )
                account = await cursor.fetchone()
                
                if not account:
                    await interaction.response.send_message(
                        f"You don't have a debt account named '{account_name}'.",
                        ephemeral=True
                    )
                    return
                    
                account_id, current_balance = account
                
                # Calculate the new balance
                new_balance = current_balance + amount
                now = datetime.now().isoformat()
                
                # Record the charge as a negative payment
                await db.execute(
                    'INSERT INTO debt_payments (account_id, amount, payment_date, description) VALUES (?, ?, ?, ?)',
                    (account_id, -amount, now, description or f"Charge/Fee: ${amount:,.2f}")
                )
                
                # Update the current balance
                await db.execute(
                    'UPDATE debt_accounts SET current_balance = ?, updated_at = ? WHERE id = ?',
                    (new_balance, now, account_id)
                )
                
                await db.commit()
                
                # Send confirmation message
                await interaction.response.send_message(
                    f"Added a charge of ${amount:,.2f} to '{account_name}'. New balance: ${new_balance:,.2f}",
                    ephemeral=True
                )
                
                # Update the debt dashboard
                await self.bot.update_debt_dashboard()
                
        except Exception as e:
            logging.error(f"Error adding charge to debt account: {e}")
            await interaction.response.send_message(
                "An error occurred while adding the charge to your debt account. Please try again.",
                ephemeral=True
            )

async def setup(bot):
  bot.tree.add_command(DebtCommands(bot))