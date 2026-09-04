"""MightPulse API client and Discord commands for Kingshot data."""
import asyncio
import logging
import os
import sqlite3
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands
from .alliance import resolve_alliance_kid
from .alliance_member_edit import remove_member_from_alliance, upsert_api_member
from .permission_handler import PermissionManager
from .pimp_my_bot import theme, safe_edit_message

logger = logging.getLogger("mightpulse")
MIGHTPULSE_SETTINGS_DB = "db/settings.sqlite"
MIGHTPULSE_API_KEY_SETTING = "api_key"
MIGHTPULSE_LOG_FILE = "log/mightpulse.txt"
MIGHTPULSE_API_BASE_URL = os.getenv(
    "MIGHTPULSE_API_BASE_URL", "https://api.mightpulse.com"
).rstrip("/")


def _configure_mightpulse_logging():
    """Attach the MightPulse file handler once, including during cog reloads."""
    os.makedirs(os.path.dirname(MIGHTPULSE_LOG_FILE), exist_ok=True)
    log_path = os.path.abspath(MIGHTPULSE_LOG_FILE)
    if not any(
        isinstance(handler, logging.FileHandler)
        and os.path.abspath(handler.baseFilename) == log_path
        for handler in logger.handlers
    ):
        handler = logging.FileHandler(MIGHTPULSE_LOG_FILE, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s"
        ))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


_configure_mightpulse_logging()


def get_mightpulse_api_key() -> str | None:
    """Return the stored MightPulse API key, or None when it is not configured."""
    try:
        with sqlite3.connect(MIGHTPULSE_SETTINGS_DB, timeout=30.0) as conn:
            row = conn.execute(
                "SELECT setting_value FROM mightpulse_settings WHERE setting_key = ?",
                (MIGHTPULSE_API_KEY_SETTING,),
            ).fetchone()
        api_key = row[0].strip() if row and row[0] else None
        logger.info("API key lookup completed: configured=%s", bool(api_key))
        return api_key
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error).lower():
            logger.warning("Could not read MightPulse API key: %s", error)
        logger.warning("API key lookup failed: %s", error)
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
    logger.info("MightPulse API key saved successfully")


class MightpulseApiKeyModal(discord.ui.Modal, title="Set MightPulse API Key"):
    """Collect and persist the MightPulse API key without posting it in chat."""

    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.api_key_input = discord.ui.TextInput(
            label="MightPulse API key",
            placeholder="Paste your API key",
            required=True,
            min_length=1,
            max_length=4000,
        )
        self.add_item(self.api_key_input)

    async def on_submit(self, interaction: discord.Interaction):
        logger.info("API key modal submitted by user_id=%s", interaction.user.id)
        is_admin, is_global = PermissionManager.is_admin(interaction.user.id)
        if not is_admin or not is_global:
            await interaction.response.send_message(
                "Only global administrators can change the MightPulse API key.",
                ephemeral=True,
            )
            return

        api_key = self.api_key_input.value.strip()
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

        logger.info("API key modal accepted for user_id=%s", interaction.user.id)
        await self.cog.show_mightpulse_menu(interaction)


class MightpulseMenuView(discord.ui.View):
    """Navigation controls for the MightPulse menu."""

    def __init__(self, cog):
        super().__init__(timeout=7200)
        self.cog = cog

    @discord.ui.button(
        label="Synchronize Alliance",
        style=discord.ButtonStyle.primary,
        custom_id="mightpulse_synchronize_alliance",
    )
    async def synchronize_alliance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info("Synchronization menu opened by user_id=%s", interaction.user.id)
        await self.cog.show_alliance_selector(interaction)

    @discord.ui.button(
        label="Set API Key",
        style=discord.ButtonStyle.primary,
        custom_id="mightpulse_set_api_key",
    )
    async def set_api_key_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info("API key modal opened by user_id=%s", interaction.user.id)
        await interaction.response.send_modal(MightpulseApiKeyModal(self.cog))

    @discord.ui.button(
        label="Main Menu",
        emoji=theme.homeIcon,
        style=discord.ButtonStyle.secondary,
        custom_id="mightpulse_back",
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

    async def show_mightpulse_menu(self, interaction: discord.Interaction,
                                   notice: str | None = None, message=None,
                                   result_only: bool = False, result_title: str | None = None):
        """Show the MightPulse status until data-specific actions are added."""
        status = "Configured" if self.get_api_key() else "Not configured"
        description = f"API key status: **{status}**\n\nData tools will be added here."
        if notice:
            description = notice if result_only else f"{notice}\n\n{description}"
        if result_only:
            result_embed = discord.Embed(
                title=result_title or "Synchronization Result",
                description=notice or "Synchronization completed.",
                color=theme.emColor1,
            )
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.warning("Could not remove the synchronization progress message")
            await interaction.followup.send(
                embed=result_embed,
                ephemeral=True,
            )
            return

        view = MightpulseMenuView(self)
        embed = discord.Embed(
            title=f"{theme.globeIcon} MightPulse",
            description=description,
            color=theme.emColor1,
        )
        if message is not None:
            await message.edit(embed=embed, view=view, content=None)
        else:
            await safe_edit_message(
                interaction,
                embed=embed,
                view=view,
                content=None,
            )

    async def show_alliance_selector(self, interaction: discord.Interaction):
        """Show configured alliances for the user to select for synchronization."""
        try:
            with sqlite3.connect("db/alliance.sqlite", timeout=30.0) as conn:
                alliances = conn.execute(
                    "SELECT alliance_id, name FROM alliance_list ORDER BY name"
                ).fetchall()
        except sqlite3.Error:
            logger.exception("Could not load alliances for MightPulse synchronization")
            await self.show_mightpulse_menu(
                interaction,
                f"{theme.deniedIcon} Could not load the configured alliances.",
            )
            return

        if not alliances:
            logger.info("Alliance selector requested but no alliances are configured")
            await self.show_mightpulse_menu(
                interaction,
                f"{theme.deniedIcon} No alliances are configured yet.",
            )
            return

        logger.info("Loaded %s configured alliances for synchronization", len(alliances))

        options = [
            discord.SelectOption(label=name[:100], value=str(alliance_id))
            for alliance_id, name in alliances[:25]
        ]
        view = MightpulseAllianceSelectorView(self, options)
        embed = discord.Embed(
            title=f"{theme.globeIcon} Synchronize Alliance",
            description="Choose an alliance to synchronize with MightPulse.",
            color=theme.emColor1,
        )
        await safe_edit_message(interaction, embed=embed, view=view, content=None)

    async def restore_main_menu(self, message):
        """Restore the original selector message to the main menu."""
        logger.info("Restoring original message to Main Menu")
        main_menu = self.bot.get_cog("MainMenu")
        if main_menu is None:
            logger.warning("Could not restore original message: MainMenu cog unavailable")
            return
        from .bot_main_menu import MainMenuView

        embed = main_menu.build_main_menu_embed()
        try:
            await message.edit(embed=embed, view=MainMenuView(main_menu))
            logger.info("Original message restored to Main Menu")
        except Exception:
            logger.exception("Could not restore the original MightPulse menu message")

    async def synchronize_alliance(self, interaction: discord.Interaction,
                                   alliance_id: int, kingdom_id: int,
                                   sync_message):
        """Fetch the alliance roster and synchronize it into the local alliance."""
        logger.info(
            "Synchronization started: alliance_id=%s kingdom_id=%s user_id=%s",
            alliance_id, kingdom_id, interaction.user.id,
        )
        api_key = self.get_api_key()
        logger.info("API key availability checked: configured=%s", bool(api_key))
        if not api_key:
            await self.show_mightpulse_menu(
                interaction,
                f"{theme.deniedIcon} Set the MightPulse API key before synchronizing.",
                message=sync_message,
            )
            return

        with sqlite3.connect("db/alliance.sqlite", timeout=30.0) as conn:
            row = conn.execute(
                "SELECT name AS alliance_tag FROM alliance_list WHERE alliance_id = ?",
                (alliance_id,),
            ).fetchone()
        if not row:
            await self.show_mightpulse_menu(
                interaction,
                f"{theme.deniedIcon} The selected alliance no longer exists.",
                message=sync_message,
            )
            return

        logger.info("Alliance resolved: alliance_id=%s tag=%s", alliance_id, row[0])

        alliance_tag = quote(str(row[0]), safe="")
        endpoint = (
            f"{MIGHTPULSE_API_BASE_URL}/v1/alliances/{kingdom_id}/{alliance_tag}"
            "?include=info,roster"
        )
        logger.info("Requesting alliance roster: alliance_id=%s endpoint=%s", alliance_id, endpoint)
        try:
            logger.info("Opening MightPulse HTTP session: alliance_id=%s", alliance_id)
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                logger.info("Sending MightPulse roster request: alliance_id=%s", alliance_id)
                async with session.get(
                    endpoint,
                    headers={"X-API-Key": api_key, "Accept": "application/json"},
                ) as response:
                    if response.status != 200:
                        body = (await response.text())[:200]
                        raise RuntimeError(
                            f"MightPulse returned HTTP {response.status}: {body}"
                        )
                    logger.info(
                        "MightPulse roster request succeeded: alliance_id=%s status=%s",
                        alliance_id, response.status,
                    )
                    payload = await response.json(content_type=None)
                    logger.info("MightPulse response JSON decoded: alliance_id=%s", alliance_id)
        except Exception as error:
            logger.warning("MightPulse synchronization failed: %s", error)
            try:
                await self.show_mightpulse_menu(
                    interaction,
                    f"{theme.deniedIcon} Could not synchronize **{row[0]}**: {error}",
                    message=sync_message,
                )
            except Exception:
                logger.exception("Could not display MightPulse synchronization error")
                await interaction.followup.send(
                    f"{theme.deniedIcon} Could not synchronize **{row[0]}**.",
                    ephemeral=True,
                )
            return

        members = payload.get("members") if isinstance(payload, dict) else None
        if not isinstance(members, list):
            await self.show_mightpulse_menu(
                interaction,
                f"{theme.deniedIcon} MightPulse returned no valid member roster.",
                message=sync_message,
            )
            return

        logger.info(
            "Roster received: alliance_id=%s api_member_count=%s",
            alliance_id, len(members),
        )

        with sqlite3.connect("db/users.sqlite", timeout=30.0) as conn:
            configured_users = {
                str(fid): (fid, nickname)
                for fid, nickname in conn.execute(
                    "SELECT fid, nickname FROM users WHERE alliance = ?",
                    (str(alliance_id),),
                ).fetchall()
            }
        logger.info(
            "Loaded configured local members: alliance_id=%s count=%s",
            alliance_id, len(configured_users),
        )

        added_names = []
        changed_names = []
        failed_names = []
        api_fids = set()
        unchanged = 0
        for member in members:
            if not isinstance(member, dict):
                failed_names.append("Unknown player")
                continue
            governor_id = member.get("governor_id")
            if governor_id is None or member.get("nick_name") is None:
                failed_names.append(str(governor_id or "Unknown player"))
                continue
            api_fids.add(str(governor_id))
            member_name = str(member["nick_name"]).strip() or str(governor_id)
            try:
                was_added, changed = await asyncio.to_thread(
                    upsert_api_member,
                    governor_id,
                    member.get("nick_name"),
                    member.get("town_center_level", 0),
                    alliance_id,
                    kingdom_id,
                )
                if was_added:
                    added_names.append(member_name)
                elif changed:
                    changed_names.append(member_name)
                else:
                    unchanged += 1
                if (len(added_names) + len(changed_names) + unchanged) % 10 == 0:
                    logger.info(
                        "Member synchronization progress: alliance_id=%s processed=%s",
                        alliance_id, len(added_names) + len(changed_names) + unchanged,
                    )
            except (TypeError, ValueError, sqlite3.Error):
                logger.exception("Could not synchronize MightPulse member %s", governor_id)
                failed_names.append(member_name)

        missing_users = [
            (fid, nickname)
            for fid, nickname in configured_users.values()
            if str(fid) not in api_fids
        ]

        if missing_users:
            logger.info(
                "Missing configured members detected: alliance_id=%s count=%s",
                alliance_id, len(missing_users),
            )
            removal_view = MightpulseRemovalView(
                self, alliance_id, row[0], missing_users,
                added_names, changed_names, unchanged, failed_names, sync_message,
            )
            removal_embed = removal_view.build_embed()
            await safe_edit_message(
                interaction, embed=removal_embed, view=removal_view, content=None
            )
            return

        await self.finish_synchronization(
            interaction, row[0], added_names, changed_names, unchanged, [],
            failed_names, sync_message,
        )

    async def finish_synchronization(self, interaction, alliance_name, added_names,
                                     changed_names, unchanged, removed_names,
                                     failed_names, sync_message=None):
        """Apply the selected removals and display the completed sync report."""
        def report_section(title, names=None, count=None):
            if names is not None:
                if not names:
                    return f"{title} (0)"
                return f"{title} ({len(names)})\n{', '.join(names)}"
            return f"{title}({count})"

        report = "\n\n".join((
            report_section("Added", added_names),
            report_section("Changed", changed_names),
            report_section("Unchanged", count=unchanged),
            report_section("Removed", removed_names),
            report_section("Failed", failed_names),
        ))

        logger.info(
            "Synchronization finished: alliance=%s added=%s changed=%s "
            "unchanged=%s removed=%s failed=%s",
            alliance_name, len(added_names), len(changed_names), unchanged,
            len(removed_names), len(failed_names),
        )

        await self.show_mightpulse_menu(
            interaction,
            report,
            message=sync_message,
            result_only=True,
            result_title=f"{theme.verifiedIcon} Synchronized **{alliance_name}**",
        )


class MightpulseRemovalView(discord.ui.View):
    """Paginated multiselect for configured members missing from the API roster."""

    def __init__(self, cog, alliance_id, alliance_name, missing_users,
                 added_names, changed_names, unchanged, failed_names, sync_message):
        super().__init__(timeout=7200)
        self.cog = cog
        self.alliance_id = alliance_id
        self.alliance_name = alliance_name
        self.missing_users = missing_users
        self.added_names = added_names
        self.changed_names = changed_names
        self.unchanged = unchanged
        self.failed_names = failed_names
        self.sync_message = sync_message
        self.page = 0
        self.selected_fids = set()
        self._build_components()

    def _build_components(self):
        self.clear_items()
        start = self.page * 25
        page_users = self.missing_users[start:start + 25]
        options = [
            discord.SelectOption(
                label=str(nickname or f"Player {fid}")[:100],
                value=str(fid),
                description=f"ID: {fid}",
                default=fid in self.selected_fids,
            )
            for fid, nickname in page_users
        ]
        select = discord.ui.Select(
            placeholder=(
                f"Select players to remove (Page {self.page + 1}/"
                f"{self.page_count})"
            ),
            options=options,
            min_values=0,
            max_values=len(options),
            custom_id="mightpulse_removal_select",
        )
        select.callback = self._select_players
        self.add_item(select)

        if self.page_count > 1:
            previous = discord.ui.Button(
                label="",
                emoji=theme.prevIcon,
                style=discord.ButtonStyle.secondary,
                disabled=self.page == 0,
            )
            previous.callback = self._previous_page
            self.add_item(previous)
            next_button = discord.ui.Button(
                label="",
                emoji=theme.nextIcon,
                style=discord.ButtonStyle.secondary,
                disabled=self.page == self.page_count - 1,
            )
            next_button.callback = self._next_page
            self.add_item(next_button)

        remove_all = discord.ui.Button(
            label="Remove All", style=discord.ButtonStyle.danger,
        )
        remove_all.callback = self._remove_all
        self.add_item(remove_all)
        remove_selected = discord.ui.Button(
            label="Remove Selected", style=discord.ButtonStyle.danger,
            disabled=not self.selected_fids,
        )
        remove_selected.callback = self._remove_selected
        self.add_item(remove_selected)
        skip = discord.ui.Button(
            label="Skip", style=discord.ButtonStyle.secondary,
        )
        skip.callback = self._skip
        self.add_item(skip)

    @property
    def page_count(self):
        return (len(self.missing_users) + 24) // 25

    def build_embed(self):
        return discord.Embed(
            title=f"{theme.warnIcon} Players Missing From Alliance",
            description=(
                f"These configured players were not returned by MightPulse for "
                f"**{self.alliance_name}**.\n\n"
                f"Select players to remove, remove all missing players, or skip.\n"
                f"**Selected:** `{len(self.selected_fids)}` / `{len(self.missing_users)}`"
            ),
            color=theme.emColor2,
        )

    async def _select_players(self, interaction):
        page_fids = {fid for fid, _ in self.missing_users[self.page * 25:self.page * 25 + 25]}
        self.selected_fids -= page_fids
        self.selected_fids.update(int(fid) for fid in interaction.data.get("values", []))
        self._build_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _previous_page(self, interaction):
        self.page -= 1
        self._build_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next_page(self, interaction):
        self.page += 1
        self._build_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _remove_all(self, interaction):
        await self._finish(interaction, {fid for fid, _ in self.missing_users})

    async def _remove_selected(self, interaction):
        await self._finish(interaction, self.selected_fids)

    async def _skip(self, interaction):
        await self._finish(interaction, set())

    async def _finish(self, interaction, fids_to_remove):
        logger.info(
            "Removal decision: alliance=%s selected_count=%s missing_count=%s",
            self.alliance_name, len(fids_to_remove), len(self.missing_users),
        )
        removed_names = []
        failed_names = list(self.failed_names)
        name_by_fid = {fid: nickname for fid, nickname in self.missing_users}
        for fid in fids_to_remove:
            try:
                removed = await asyncio.to_thread(
                    remove_member_from_alliance, fid, self.alliance_id
                )
                if removed:
                    removed_names.append(str(name_by_fid.get(fid) or f"Player {fid}"))
            except sqlite3.Error:
                logger.exception("Could not remove missing MightPulse member %s", fid)
                failed_names.append(str(name_by_fid.get(fid) or f"Player {fid}"))

        self.stop()
        await self.cog.finish_synchronization(
            interaction, self.alliance_name, self.added_names, self.changed_names,
            self.unchanged, removed_names, failed_names, self.sync_message,
        )


class MightpulseAllianceSelectorView(discord.ui.View):
    """Alliance selector used before starting MightPulse synchronization."""

    def __init__(self, cog, options):
        super().__init__(timeout=7200)
        self.cog = cog
        self.options = options
        self.selected_alliance_id = None
        self.select = discord.ui.Select(
            placeholder="Select an alliance",
            options=options,
            custom_id="mightpulse_select_alliance",
        )
        self.select.callback = self.select_alliance
        self.add_item(self.select)

        synchronize_button = discord.ui.Button(
            label="Synchronize",
            style=discord.ButtonStyle.primary,
            custom_id="mightpulse_start_synchronization",
            disabled=True,
        )
        synchronize_button.callback = self.start_synchronization
        self.synchronize_button = synchronize_button
        self.add_item(synchronize_button)

        back_button = discord.ui.Button(
            label="Back",
            emoji=theme.backIcon,
            style=discord.ButtonStyle.secondary,
            custom_id="mightpulse_selector_back",
        )
        back_button.callback = self.go_back
        self.add_item(back_button)

    async def select_alliance(self, interaction: discord.Interaction):
        values = interaction.data.get("values", [])
        if not values:
            return
        self.selected_alliance_id = int(values[0])
        self.synchronize_button.disabled = False
        selected_name = next(
            (option.label for option in self.options
             if option.value == str(self.selected_alliance_id)),
            str(self.selected_alliance_id),
        )
        self.select.placeholder = selected_name
        for option in self.select.options:
            option.default = option.value == str(self.selected_alliance_id)
        embed = discord.Embed(
            title=f"{theme.globeIcon} Synchronize Alliance",
            description=(
                "Press **Synchronize** to start the synchronization."
            ),
            color=theme.emColor1,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def start_synchronization(self, interaction: discord.Interaction):
        logger.info(
            "Synchronize button pressed: alliance_id=%s user_id=%s",
            self.selected_alliance_id, interaction.user.id,
        )
        if self.selected_alliance_id is None:
            await interaction.response.send_message(
                f"{theme.deniedIcon} Select an alliance first.", ephemeral=True
            )
            return

        logger.info("Deferring synchronization interaction")
        await interaction.response.defer()
        logger.info("Creating synchronization progress message in channel")
        if interaction.channel is None:
            logger.error("Cannot create synchronization message: interaction has no channel")
            await interaction.followup.send(
                f"{theme.deniedIcon} Could not create the synchronization message.",
                ephemeral=True,
            )
            return
        sync_message = await interaction.channel.send(
            embed=discord.Embed(
                title=f"{theme.globeIcon} Synchronizing Alliance",
                description="Fetching the alliance roster...",
                color=theme.emColor1,
            )
        )
        logger.info("Synchronization progress message created")
        if interaction.message is not None:
            logger.info("Scheduling original menu restoration")
            asyncio.create_task(self.cog.restore_main_menu(interaction.message))
        logger.info("Continuing after synchronization progress message")

        logger.info("Resolving alliance kingdom: alliance_id=%s", self.selected_alliance_id)
        ok, kingdom_id = resolve_alliance_kid(self.selected_alliance_id)
        logger.info(
            "Alliance kingdom resolved: alliance_id=%s ok=%s kingdom_id=%s",
            self.selected_alliance_id, ok, kingdom_id,
        )
        if not ok:
            await self.cog.show_mightpulse_menu(
                interaction,
                f"{theme.deniedIcon} Could not verify the alliance kingdom right now. "
                "Please try again in a moment.",
                message=sync_message,
            )
            return
        if kingdom_id is None:
            await self.cog.show_mightpulse_menu(
                interaction,
                f"{theme.deniedIcon} This alliance needs to be kingdom locked before it can be synchronized.",
                message=sync_message,
            )
            return

        await self.cog.synchronize_alliance(
            interaction, self.selected_alliance_id, kingdom_id, sync_message
        )

    async def go_back(self, interaction: discord.Interaction):
        await self.cog.show_mightpulse_menu(interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(Mightpulse(bot))
