"""Member kingdom management, under Alliance Management -> Member Kingdoms."""

import asyncio
import logging

import discord

from .bot_level_mapping import parse_state
from .permission_handler import PermissionManager
from .pimp_my_bot import theme, safe_edit_message, check_interaction_user, notify_view_expired

logger = logging.getLogger('gift')


def _detect_lines(cog, scope, label):
    """Live detect-progress line: members checked, the current member's scan, or paused."""
    running = cog.state_resolve_status(scope)
    if not running:
        return []
    checked = running['total'] - len(running['remaining'])
    out = [f"\n{theme.hourglassIcon} {label}: `{checked}/{running['total']}` members checked, "
           f"`{running.get('resolved', 0)}` fixed."]
    if running.get('status') == 'queued' and checked < running['total']:
        out.append(f"└ Paused while gift codes redeem - it picks up where it left off.")
    elif running.get('probe_total'):
        out.append(f"└ `{running['probe']}/{running['probe_total']}` kingdoms checked "
                   f"for the member being scanned.")
    out.append("└ Detailed progress is posted in each alliance's log channel.")
    return out


def _catchup_lines(cog):
    """Live 'still redeeming missed codes' line, or nothing when no catch-up is queued."""
    catching = cog.member_catchup_status()
    if not catching:
        return []
    return [f"\n{theme.giftIcon} Redeeming missed codes: "
            f"`{catching['done']}/{catching['codes']}` for `{catching['members']}` member(s)."]


async def show_state_management(cog, interaction: discord.Interaction):
    # Every action here spans all alliances, so it's Global-only regardless of the button gate.
    _, is_global = PermissionManager.is_admin(interaction.user.id)
    if not is_global:
        await interaction.response.send_message(
            f"{theme.deniedIcon} Only global administrators can manage member kingdoms.",
            ephemeral=True,
        )
        return
    view = StateManagementView(cog, interaction.user.id)
    await safe_edit_message(interaction, embed=await view.build_embed(), view=view, content=None)


class StateManagementView(discord.ui.View):
    def __init__(self, cog, user_id):
        super().__init__(timeout=7200)
        self.cog = cog
        self.original_user_id = user_id
        self.last_result = None

    async def build_embed(self):
        from . import gift_state_resolver as gsr
        missing = await asyncio.to_thread(gsr.fids_missing_state)
        rejected = await asyncio.to_thread(gsr.fids_with_state_mismatch)
        survey = await asyncio.to_thread(gsr.survey_alliance_bindings)
        multistate = [r for r in survey if r["multistate"]]
        unbound = [r for r in survey if r["current_kid"] is None and not r["multistate"]]
        bindable = [r for r in unbound if r["proposed_kid"] is not None]

        lines = [
            "Redemption needs each member's kingdom on file. "
            "Fix wrong or missing kingdoms here.\n",
            f"{theme.upperDivider}",
            f"{theme.stateIcon} **Members with a wrong kingdom:** `{len(rejected)}`",
            f"{theme.membersIcon} **Members missing a kingdom:** `{len(missing)}`",
            f"{theme.allianceIcon} **Unbound alliances:** `{len(unbound)}` "
            f"({len(bindable)} can be auto-bound)",
            f"{theme.allianceIcon} **Multi-Kingdom alliances:** `{len(multistate)}` (never auto-bound)\n",
            f"{theme.stateIcon} **Wrong Kingdoms**",
            "└ Fix members the game API rejected during redemption.",
            f"{theme.chartIcon} **Auto-bind Alliances**",
            "└ Give each alliance the kingdom most of its members are in.",
            f"{theme.membersIcon} **Assign Kingdom to Missing**",
            "└ Give kingdom-less members their alliance's kingdom. Instant.",
            f"{theme.searchIcon} **Detect Missing Kingdoms**",
            "└ Ask the game API for kingdom-less members' kingdoms. About 30 minutes each.",
            f"{theme.allianceIcon} **Multi-Kingdom Alliances**",
            "└ Flag alliances that span many kingdoms so they're never auto-bound.",
            f"\n{theme.infoIcon} Can't auto-bind an alliance (too few known members)? "
            f"Set its kingdom on the alliance itself (Alliance Management -> Set Kingdom), then Assign Kingdom to Missing.",
        ]
        lines.extend(_detect_lines(self.cog, 'missing', "Detecting missing kingdoms"))
        lines.extend(_catchup_lines(self.cog))
        if self.last_result:
            lines.append(f"\n{theme.verifiedIcon} {self.last_result}")
        lines.append(f"{theme.lowerDivider}")

        return discord.Embed(
            title=f"{theme.stateIcon} Member Kingdoms",
            description="\n".join(lines),
            color=theme.emColor1,
        )

    @discord.ui.button(label="Wrong Kingdoms", style=discord.ButtonStyle.danger,
                       emoji=f"{theme.stateIcon}", row=0)
    async def wrong_states_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        view = WrongStateView(self.cog, self.original_user_id)
        await safe_edit_message(interaction, embed=await view.build_embed(), view=view, content=None)

    @discord.ui.button(label="Auto-bind Alliances", style=discord.ButtonStyle.primary,
                       emoji=f"{theme.chartIcon}", row=0)
    async def bind_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        result = await asyncio.to_thread(self.cog.bind_alliance_states)
        b, m = len(result["bound"]), len(result["multistate"])
        self.last_result = (
            f"Bound {b} alliance(s) to a state; flagged {m} as multistate."
            if (b or m) else "No alliances had a clear majority to bind."
        )
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    @discord.ui.button(label="Assign Kingdom to Missing", style=discord.ButtonStyle.primary,
                       emoji=f"{theme.membersIcon}", row=0)
    async def assign_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        n, caught = await asyncio.to_thread(self.cog.assign_alliance_state_to_missing)
        self.last_result = (
            f"Assigned an alliance kingdom to {n} member(s) that had none."
            + (f" Redeeming the codes {caught} of them missed." if caught else "")
        )
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    @discord.ui.button(label="Detect Missing Kingdoms", style=discord.ButtonStyle.secondary,
                       emoji=f"{theme.searchIcon}", row=1)
    async def resolve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        queued = self.cog.queue_state_resolve('missing')
        self.last_result = _queue_result_text(queued, "missing a kingdom")
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    @discord.ui.button(label="Multi-Kingdom Alliances", style=discord.ButtonStyle.secondary,
                       emoji=f"{theme.allianceIcon}", row=1)
    async def multistate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        view = MultistateView(self.cog, self.original_user_id)
        await safe_edit_message(interaction, embed=await view.build_embed(), view=view, content=None)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary,
                       emoji=f"{theme.backIcon}", row=2)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        main_menu = self.cog.bot.get_cog("MainMenu")
        if main_menu:
            await main_menu.show_alliance_management(interaction)

    async def on_timeout(self):
        await notify_view_expired(self, "member kingdoms menu")


def _queue_result_text(queued, label):
    """Shared wording for the two detect buttons."""
    if queued is None:
        return f"Already detecting kingdoms for members {label} - it's working through them."
    if queued == 0:
        return f"No members are {label}."
    return (f"Queued {queued} member(s) {label}. This runs in the background and steps aside "
            f"whenever a gift code needs redeeming, so it can take a while.")


class WrongStateView(discord.ui.View):
    """Members whose stored kingdom the game rejected during redemption (error 40020)."""
    def __init__(self, cog, user_id, page=0):
        super().__init__(timeout=7200)
        self.cog = cog
        self.original_user_id = user_id
        self.page = page
        self.rows = []
        self.last_result = None

    async def build_embed(self):
        from . import gift_state_resolver as gsr
        self.rows = await asyncio.to_thread(gsr.fids_with_state_mismatch)
        self.clear_items()

        pages = max(1, (len(self.rows) + 24) // 25)
        self.page = max(0, min(self.page, pages - 1))
        page_rows = self.rows[self.page * 25:(self.page + 1) * 25]

        if page_rows:
            options = [
                discord.SelectOption(
                    label=str(nickname or fid)[:100], value=str(fid),
                    description=f"ID {fid} - kingdom {kid} was rejected"[:100],
                )
                for fid, nickname, kid, _alliance, _flagged in page_rows
            ]
            select = discord.ui.Select(placeholder="Pick a member to set their kingdom", options=options, row=0)
            select.callback = self._on_select
            self.add_item(select)
            if pages > 1:
                self._add_nav(pages)
            self._add_button("Set All to Kingdom", theme.membersIcon,
                             discord.ButtonStyle.success, 2, self._on_set_all)
            self._add_button("Detect Kingdoms", theme.searchIcon,
                             discord.ButtonStyle.primary, 2, self._on_resolve)
            self._add_button("Clear List", theme.trashIcon,
                             discord.ButtonStyle.secondary, 2, self._on_clear)
        self._add_button("Back", theme.backIcon, discord.ButtonStyle.secondary, 3, self._on_back)

        lines = [
            "The game rejected these members' kingdoms the last time a code was redeemed for them - "
            "they transferred, or they left the game.\n",
            f"{theme.upperDivider}",
            f"{theme.stateIcon} **Members with a wrong kingdom:** `{len(self.rows)}`\n",
            f"{theme.userIcon} **Pick a member**",
            "└ Type their correct kingdom. They redeem again right away.",
            f"{theme.membersIcon} **Set All to Kingdom**",
            "└ Put every listed member in one kingdom, for a batch that transferred together.",
            f"{theme.searchIcon} **Detect Kingdoms**",
            "└ Ask the game API for each member's kingdom. About 30 minutes each, in the background.",
            f"{theme.trashIcon} **Clear List**",
            "└ Dismiss these warnings. Still-wrong members reappear after the next redemption.",
        ]
        lines.extend(_detect_lines(self.cog, 'mismatch', "Detecting kingdoms"))
        lines.extend(_catchup_lines(self.cog))
        if not self.rows:
            lines.append(f"\n{theme.verifiedIcon} No members have a wrong kingdom right now.")
        if self.last_result:
            lines.append(f"\n{theme.verifiedIcon} {self.last_result}")
        lines.append(f"{theme.lowerDivider}")

        return discord.Embed(title=f"{theme.stateIcon} Wrong Kingdoms",
                             description="\n".join(lines), color=theme.emColor1)

    def _add_button(self, label, emoji, style, row, callback, disabled=False):
        button = discord.ui.Button(label=label, emoji=f"{emoji}", style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    def _add_nav(self, pages):
        self._add_button("Prev", theme.prevIcon, discord.ButtonStyle.secondary, 1,
                         self._on_prev, disabled=self.page == 0)
        self._add_button("Next", theme.nextIcon, discord.ButtonStyle.secondary, 1,
                         self._on_next, disabled=self.page >= pages - 1)

    async def _on_select(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        fid = int(interaction.data["values"][0])
        match = next((r for r in self.rows if r[0] == fid), None)
        await interaction.response.send_modal(
            MemberStateModal(self, fid, match[1] if match else str(fid), match[2] if match else None))

    async def _on_set_all(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        fids = [r[0] for r in self.rows]
        await interaction.response.send_modal(BulkStateModal(self, fids))

    async def _on_resolve(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        queued = self.cog.queue_state_resolve('mismatch')
        self.last_result = _queue_result_text(queued, "flagged with a wrong kingdom")
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    async def _on_clear(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        from . import gift_state_resolver as gsr
        cleared = len(self.rows)
        for fid, *_ in self.rows:
            await asyncio.to_thread(gsr.clear_state_mismatch, fid)
        self.cog.logger.info(f"GiftOps: admin cleared {cleared} wrong-state flag(s)")
        self.last_result = f"Cleared {cleared} warning(s)."
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    async def _on_prev(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        self.page -= 1
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    async def _on_next(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        self.page += 1
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    async def _on_back(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        view = StateManagementView(self.cog, self.original_user_id)
        await safe_edit_message(interaction, embed=await view.build_embed(), view=view, content=None)

    async def on_timeout(self):
        await notify_view_expired(self, "wrong kingdoms menu")


class MemberStateModal(discord.ui.Modal):
    """Set one member's kingdom by hand."""
    def __init__(self, parent_view, fid, nickname, current_kid):
        super().__init__(title="Set Member Kingdom")
        self.parent_view = parent_view
        self.fid = fid
        self.nickname = nickname
        self.state = discord.ui.TextInput(
            label=f"Kingdom for {str(nickname)[:30]}",
            placeholder="The kingdom number the member is in now, e.g. 911",
            default=str(current_kid) if current_kid is not None else None,
            required=True, max_length=5,
        )
        self.add_item(self.state)

    async def on_submit(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.parent_view.original_user_id):
            return
        from . import gift_state_resolver as gsr
        kid = parse_state(self.state.value)
        if kid is None:
            await interaction.response.send_message(
                f"{theme.deniedIcon} `{self.state.value.strip()}` isn't a kingdom number. "
                f"Enter digits only, like `911`.",
                ephemeral=True)
            return
        from . import gift_redemption
        cog = self.parent_view.cog
        await asyncio.to_thread(gsr.set_user_kid, self.fid, kid)
        cog.logger.info(f"GiftOps: admin set state {kid} for FID {self.fid} ({self.nickname})")
        queued = gift_redemption.enqueue_member_redemption(cog, self.fid, self.nickname)
        self.parent_view.last_result = (
            f"{self.nickname} is now in kingdom {kid}. "
            + (f"Redeeming {queued} code(s) they missed." if queued
               else "They already have every code.")
        )
        await safe_edit_message(
            interaction, embed=await self.parent_view.build_embed(),
            view=self.parent_view, content=None)


class BulkStateModal(discord.ui.Modal):
    """Set one kingdom on every listed member - for a batch that transferred together."""
    def __init__(self, parent_view, fids):
        super().__init__(title="Set Kingdom for All")
        self.parent_view = parent_view
        self.fids = fids
        self.state = discord.ui.TextInput(
            label=f"Kingdom for all {len(fids)} member(s)",
            placeholder="The state they're all in now, e.g. 911",
            required=True, max_length=5,
        )
        self.add_item(self.state)

    async def on_submit(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.parent_view.original_user_id):
            return
        from . import gift_state_resolver as gsr
        from . import gift_redemption
        kid = parse_state(self.state.value)
        if kid is None:
            await interaction.response.send_message(
                f"{theme.deniedIcon} `{self.state.value.strip()}` isn't a kingdom number. "
                f"Enter digits only, like `911`.",
                ephemeral=True)
            return
        cog = self.parent_view.cog
        caught = 0
        for fid in self.fids:
            await asyncio.to_thread(gsr.set_user_kid, fid, kid)
            if gift_redemption.enqueue_member_redemption(cog, fid):
                caught += 1
        cog.logger.info(f"GiftOps: admin set state {kid} for {len(self.fids)} flagged member(s)")
        self.parent_view.last_result = (
            f"Set {len(self.fids)} member(s) to kingdom {kid}."
            + (f" Catching up {caught} on missed codes." if caught else "")
        )
        await safe_edit_message(
            interaction, embed=await self.parent_view.build_embed(),
            view=self.parent_view, content=None)


class MultistateView(discord.ui.View):
    """Toggle which alliances are 'multistate' (members from many states - never auto-bound)."""
    def __init__(self, cog, user_id, page=0):
        super().__init__(timeout=7200)
        self.cog = cog
        self.original_user_id = user_id
        self.page = page
        self.rows = []

    async def _load(self):
        from . import gift_state_resolver as gsr
        self.rows = await asyncio.to_thread(gsr.survey_alliance_bindings)
        self.rows.sort(key=lambda r: (not r["multistate"], str(r["name"]).lower()))

    async def build_embed(self):
        await self._load()
        self.clear_items()
        pages = max(1, (len(self.rows) + 24) // 25)
        self.page = max(0, min(self.page, pages - 1))
        page_rows = self.rows[self.page * 25:(self.page + 1) * 25]

        if page_rows:
            options = []
            for r in page_rows:
                on = r["multistate"]
                status = "Multistate" if on else (f"State {r['current_kid']}" if r["current_kid"] else "Unbound")
                options.append(discord.SelectOption(
                    label=str(r["name"])[:100], value=str(r["alliance_id"]),
                    description=f"{status} - tap to turn multistate {'off' if on else 'on'}"[:100],
                    emoji=theme.verifiedIcon if on else None,
                ))
            select = discord.ui.Select(placeholder="Toggle an alliance's multistate flag", options=options, row=0)
            select.callback = self._on_select
            self.add_item(select)

        if pages > 1:
            self._add_nav(pages)
        back = discord.ui.Button(label="Back", emoji=f"{theme.backIcon}", style=discord.ButtonStyle.secondary, row=2)
        back.callback = self._on_back
        self.add_item(back)

        lines = [
            "Mark alliances whose members span many kingdoms (public/mixed bots). "
            "Multi-Kingdom alliances are never auto-bound to one kingdom and their members "
            "keep their own resolved states.\n",
            f"{theme.upperDivider}",
            f"{theme.allianceIcon} Currently multistate: "
            f"`{sum(1 for r in self.rows if r['multistate'])}`",
            f"{theme.lowerDivider}",
        ]
        return discord.Embed(title=f"{theme.allianceIcon} Multi-Kingdom Alliances",
                             description="\n".join(lines), color=theme.emColor1)

    def _add_nav(self, pages):
        prev = discord.ui.Button(label="Prev", style=discord.ButtonStyle.secondary, row=1, disabled=self.page == 0)
        nxt = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, row=1, disabled=self.page >= pages - 1)
        prev.callback = self._on_prev
        nxt.callback = self._on_next
        self.add_item(prev)
        self.add_item(nxt)

    async def _on_select(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        from . import gift_state_resolver as gsr
        alliance_id = int(interaction.data["values"][0])
        currently = await asyncio.to_thread(gsr.is_multistate, alliance_id)
        await asyncio.to_thread(gsr.set_multistate, alliance_id, not currently)
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    async def _on_prev(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        self.page -= 1
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    async def _on_next(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        self.page += 1
        await safe_edit_message(interaction, embed=await self.build_embed(), view=self, content=None)

    async def _on_back(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.original_user_id):
            return
        view = StateManagementView(self.cog, self.original_user_id)
        await safe_edit_message(interaction, embed=await view.build_embed(), view=view, content=None)

    async def on_timeout(self):
        await notify_view_expired(self, "multi-kingdom alliances menu")
