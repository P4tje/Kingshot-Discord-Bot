"""MightPulse API client and Discord commands for Kingshot data."""
import logging
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands
from .permission_handler import PermissionManager
from .pimp_my_bot import theme, safe_edit_message

logger = logging.getLogger("mightpulse")
MIGHTPULSE_SETTINGS_DB = "db/settings.sqlite"
MIGHTPULSE_API_KEY_SETTING = "api_key"


def get_mightpulse_api_key() -> str | None:
    """Return the stored MightPulse API key, or None when it is not configured."""
    try:
        with sqlite3.connect(MIGHTPULSE_SETTINGS_DB, timeout=30.0) as conn:
            row = conn.execute(
                "SELECT setting_value FROM mightpulse_settings WHERE setting_key = ?",
                (MIGHTPULSE_API_KEY_SETTING,),
            ).fetchone()
        return row[0].strip() if row and row[0] else None
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error).lower():
            logger.warning("Could not read MightPulse API key: %s", error)
        return None


def save_mightpulse_api_key(api_key: str) -> None:
    """Persist the MightPulse API key in the shared settings database."""
    with sqlite3.connect(MIGHTPULSE_SETTINGS_DB, timeout=30.0) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS mightpulse_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO mightpulse_settings (setting_key, setting_value)
               VALUES (?, ?)
               ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value""",
            (MIGHTPULSE_API_KEY_SETTING, api_key.strip()),
        )


class MightpulseMenuView(discord.ui.View):
    """Navigation controls for the MightPulse menu."""

    def __init__(self, cog):
        super().__init__(timeout=7200)
        self.cog = cog

    @discord.ui.button(
        label="Main Menu",
        emoji=theme.homeIcon,
        style=discord.ButtonStyle.secondary,
        custom_id="mightpulse_main_menu",
    )
    async def main_menu_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        main_menu = self.cog.bot.get_cog("MainMenu")
        if main_menu:
            await main_menu.show_main_menu(interaction)
        else:
            await interaction.response.send_message(
                f"{theme.deniedIcon} Main Menu module not found.",
                ephemeral=True,
            )


class Mightpulse(commands.Cog):
    """MightPulse configuration. Data endpoints will be added incrementally."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def get_api_key() -> str | None:
        """Fetch the current MightPulse API key from the database."""
        return get_mightpulse_api_key()

    async def show_mightpulse_menu(self, interaction: discord.Interaction):
        """Show the MightPulse status until data-specific actions are added."""
        status = "Configured" if self.get_api_key() else "Not configured"
        embed = discord.Embed(
            title=f"{theme.globeIcon} MightPulse",
            description=f"API key status: **{status}**\n\nData tools will be added here.",
            color=theme.emColor1,
        )
        await safe_edit_message(
            interaction,
            embed=embed,
            view=MightpulseMenuView(self),
            content=None,
        )

    @app_commands.command(name="mightpulse_set_api_key", description="Save the MightPulse API key.")
    @app_commands.describe(api_key="Your MightPulse API key")
    async def mightpulse_set_api_key(self, interaction: discord.Interaction, api_key: str):
        """Set the shared API key; only global admins may change it."""
        is_admin, is_global = PermissionManager.is_admin(interaction.user.id)
        if not is_admin or not is_global:
            await interaction.response.send_message(
                "Only global administrators can change the MightPulse API key.",
                ephemeral=True,
            )
            return

        api_key = api_key.strip()
        if not api_key:
            await interaction.response.send_message(
                "The MightPulse API key cannot be empty.",
                ephemeral=True,
            )
            return

        try:
            save_mightpulse_api_key(api_key)
        except sqlite3.Error:
            logger.exception("Could not save MightPulse API key")
            await interaction.response.send_message(
                "Could not save the MightPulse API key right now.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "MightPulse API key saved in the bot settings database.",
            ephemeral=True,
        )



async def setup(bot: commands.Bot):
    await bot.add_cog(Mightpulse(bot))
