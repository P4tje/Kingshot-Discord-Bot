"""Editing member names, Town Center levels and kingdoms by hand.

The player API no longer returns names or levels, so admins maintain them here.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime

import discord

from .bot_level_mapping import format_furnace_level, parse_furnace_level, parse_state
from .gift_state_resolver import set_user_kid
from .pimp_my_bot import theme, safe_edit_message, check_interaction_user

logger = logging.getLogger(__name__)

PLACEHOLDER_PREFIX = "Player "


def is_placeholder_name(nickname, fid) -> bool:
    """True when a member still carries the auto-generated 'Player <id>' name."""
    return str(nickname or "").strip() == f"{PLACEHOLDER_PREFIX}{fid}"


def _log_change(table: str, fid, old, new):
    """Record a manual edit in the same history the sync used to write."""
    try:
        with sqlite3.connect('db/changes.sqlite', timeout=30.0) as conn:
            columns = ("old_nickname", "new_nickname") if table == "nickname_changes" \
                else ("old_furnace_lv", "new_furnace_lv")
            conn.execute(
                f"INSERT INTO {table} (fid, {columns[0]}, {columns[1]}, change_date) "
                f"VALUES (?, ?, ?, ?)",
                (fid, old, new, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"Member edit: could not write {table} history for {fid}: {e}")


def apply_member_edit(fid, *, nickname=None, furnace_lv=None, kid=None, alliance_id=None):
    """Write a member's name/level/state; returns the fields that changed.
    `alliance_id` scopes the edit so an admin can't touch another alliance's member."""
    changed = []
    with sqlite3.connect('db/users.sqlite', timeout=30.0) as conn:
        if alliance_id is None:
            row = conn.execute(
                "SELECT nickname, furnace_lv, kid FROM users WHERE fid = ?", (fid,)).fetchone()
        else:
            row = conn.execute(
                "SELECT nickname, furnace_lv, kid FROM users WHERE fid = ? AND alliance = ?",
                (fid, str(alliance_id))).fetchone()
        if not row:
            return changed
        old_nickname, old_furnace, old_kid = row

        if nickname is not None and nickname != old_nickname:
            conn.execute("UPDATE users SET nickname = ? WHERE fid = ?", (nickname, fid))
            changed.append("name")
        if furnace_lv is not None and furnace_lv != old_furnace:
            conn.execute("UPDATE users SET furnace_lv = ? WHERE fid = ?", (furnace_lv, fid))
            changed.append("level")
        if kid is not None and kid != old_kid:
            set_user_kid(fid, kid, conn=conn)
            changed.append("state")
        conn.commit()

    if "name" in changed:
        _log_change("nickname_changes", fid, old_nickname, nickname)
        logger.info(f"Member edit: {fid} renamed '{old_nickname}' -> '{nickname}'")
    if "level" in changed:
        _log_change("furnace_changes", fid, old_furnace, furnace_lv)
        logger.info(f"Member edit: {fid} furnace {old_furnace} -> {furnace_lv}")
    if "state" in changed:
        logger.info(f"Member edit: {fid} state {old_kid} -> {kid}")
    return changed


def upsert_api_member(fid, nickname, furnace_lv, alliance_id, kid=None):
    """Add an API member or update the existing member through the member-edit path."""
    fid = int(fid)
    nickname = str(nickname or f"Player {fid}").strip()
    furnace_lv = parse_furnace_level(furnace_lv)
    if furnace_lv is None:
        furnace_lv = 0

    with sqlite3.connect('db/users.sqlite', timeout=30.0) as conn:
        row = conn.execute(
            "SELECT alliance FROM users WHERE fid = ?", (fid,)
        ).fetchone()

    if row:
        changed = apply_member_edit(
            fid,
            nickname=nickname,
            furnace_lv=furnace_lv,
            kid=kid,
        )
        with sqlite3.connect('db/users.sqlite', timeout=30.0) as conn:
            conn.execute(
                "UPDATE users SET alliance = ? WHERE fid = ?",
                (str(alliance_id), fid),
            )
            conn.commit()
        return False, bool(changed or str(row[0]) != str(alliance_id))

    with sqlite3.connect('db/users.sqlite', timeout=30.0) as conn:
        conn.execute(
                """INSERT INTO users (fid, nickname, furnace_lv, kid, stove_lv_content, alliance)
                    VALUES (?, ?, ?, ?, NULL, ?)""",
                (fid, nickname, furnace_lv, kid, str(alliance_id)),
        )
        conn.commit()
    return True, True


def remove_member_from_alliance(fid, alliance_id):
    """Remove a member from an alliance while preserving their user record."""
    with sqlite3.connect('db/users.sqlite', timeout=30.0) as conn:
        cursor = conn.execute(
            "UPDATE users SET alliance = NULL WHERE fid = ? AND alliance = ?",
            (int(fid), str(alliance_id)),
        )
        conn.commit()
    return cursor.rowcount > 0


def enqueue_catchups(bot, fids):
    """Queue owed-code redemption for members whose state just changed; returns how many."""
    fids = list(dict.fromkeys(fids))
    if not fids:
        return 0
    gift_cog = bot.get_cog('GiftOperations')
    if not gift_cog:
        return 0
    from . import gift_redemption
    caught = 0
    for fid in fids:
        try:
            if gift_redemption.enqueue_member_redemption(gift_cog, fid):
                caught += 1
        except Exception as e:
            logger.warning(f"Member edit: could not queue catch-up for {fid}: {e}")
    return caught


def parse_edit_line(line):
    """`id, name, level, state` -> (fid, nickname, furnace_lv, kid), or an error string.
    Every field after the id is optional."""
    parts = [p.strip() for p in line.split(",")]
    fid = parts[0]
    if not fid.isdigit():
        return f"`{line.strip()[:60]}` - doesn't start with a player ID"
    nickname = parts[1] if len(parts) > 1 and parts[1] else None
    furnace_lv = None
    if len(parts) > 2 and parts[2]:
        furnace_lv = parse_furnace_level(parts[2])
        if furnace_lv is None:
            return f"`{fid}` - `{parts[2][:30]}` isn't a Town Center level (try `80` or `TC 10`)"
    kid = None
    if len(parts) > 3 and parts[3]:
        kid = parse_state(parts[3])
        if kid is None:
            return f"`{fid}` - `{parts[3][:30]}` isn't a kingdom number (try `911`)"
    return (fid, nickname, furnace_lv, kid)


def apply_edit_lines(text, alliance_id=None):
    """Apply `id, name, level, state` lines -> (updated, skipped, errors, state_fids).
    state_fids are the members needing a code catch-up."""
    updated, skipped, errors, state_fids = 0, 0, [], []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parsed = parse_edit_line(line)
        if isinstance(parsed, str):
            errors.append(parsed)
            continue
        fid, nickname, furnace_lv, kid = parsed
        if nickname is None and furnace_lv is None and kid is None:
            skipped += 1
            continue
        changed = apply_member_edit(fid, nickname=nickname, furnace_lv=furnace_lv,
                                    kid=kid, alliance_id=alliance_id)
        if changed:
            updated += 1
            if "state" in changed:
                state_fids.append(fid)
        else:
            skipped += 1
    return updated, skipped, errors, state_fids


def edit_result_embed(title, updated, skipped, errors, *, added=None, caught=0):
    """Shared outcome embed for the single, bulk and CSV edit paths."""
    lines = [f"{theme.upperDivider}"]
    if added is not None:
        lines.append(f"{theme.addIcon} **Members added:** `{added}`")
    lines.append(f"{theme.verifiedIcon} **Members updated:** `{updated}`")
    if caught:
        lines.append(f"{theme.giftIcon} **Catching up on codes:** `{caught}` member(s) with a new state")
    if skipped:
        lines.append(f"{theme.infoIcon} **Unchanged:** `{skipped}` (already matched, or nothing to set)")
    if errors:
        shown = errors[:10]
        lines.append(f"{theme.deniedIcon} **Couldn't read {len(errors)} line(s):**")
        lines.extend(f"└ {e}" for e in shown)
        if len(errors) > len(shown):
            lines.append(f"└ ...and {len(errors) - len(shown)} more")
    lines.append(f"{theme.lowerDivider}")
    return discord.Embed(
        title=title,
        description="\n".join(lines),
        color=theme.emColor2 if errors and not updated else theme.emColor1,
    )


class MemberEditModal(discord.ui.Modal):
    """Edit one member's name, Town Center level and kingdom."""

    def __init__(self, parent_view, fid, nickname, furnace_lv, alliance_id, kid=None):
        super().__init__(title="Edit Member")
        self.parent_view = parent_view
        self.fid = fid
        self.alliance_id = alliance_id

        current_name = "" if is_placeholder_name(nickname, fid) else str(nickname or "")
        self.name_input = discord.ui.TextInput(
            label="Name",
            placeholder="The player's in-game name",
            default=current_name,
            required=False,
            max_length=100,
        )
        self.level_input = discord.ui.TextInput(
            label="Town Center level (optional)",
            placeholder="e.g. TC 10-2, or 82. Leave blank to keep it as it is.",
            default=format_furnace_level(furnace_lv) if furnace_lv else "",
            required=False,
            max_length=20,
        )
        self.state_input = discord.ui.TextInput(
            label="Kingdom (optional)",
            placeholder="e.g. 911. Leave blank to keep it as it is.",
            default=str(kid) if kid else "",
            required=False,
            max_length=5,
        )
        self.add_item(self.name_input)
        self.add_item(self.level_input)
        self.add_item(self.state_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.parent_view.author_id):
            return
        nickname = self.name_input.value.strip() or None
        raw_level = self.level_input.value.strip()
        furnace_lv = parse_furnace_level(raw_level) if raw_level else None
        if raw_level and furnace_lv is None:
            await interaction.response.send_message(
                f"{theme.deniedIcon} `{raw_level}` isn't a Town Center level. "
                f"Try a number like `82`, or the in-game name like `TC 10-2`.",
                ephemeral=True)
            return
        raw_state = self.state_input.value.strip()
        kid = parse_state(raw_state) if raw_state else None
        if raw_state and kid is None:
            await interaction.response.send_message(
                f"{theme.deniedIcon} `{raw_state}` isn't a kingdom number. Enter digits only, like `911`.",
                ephemeral=True)
            return

        changed = await asyncio.to_thread(
            apply_member_edit, self.fid, nickname=nickname, furnace_lv=furnace_lv,
            kid=kid, alliance_id=self.alliance_id)
        note = f"Nothing changed for `{self.fid}`."
        if changed:
            note = f"Updated {', '.join(changed)} for `{self.fid}`."
            if "state" in changed and enqueue_catchups(self.parent_view.cog.bot, [self.fid]):
                note += " Redeeming the codes they missed."
        await self.parent_view.refresh_after_edit(interaction, note)


class BulkMemberEditModal(discord.ui.Modal):
    """Edit a whole list of members in one go, one `id, name, level, kingdom` per line."""

    def __init__(self, parent_view, alliance_id, prefill=""):
        super().__init__(title="Bulk Edit Members")
        self.parent_view = parent_view
        self.alliance_id = alliance_id
        self.lines_input = discord.ui.TextInput(
            label="Per line: id, name, level, kingdom",
            style=discord.TextStyle.paragraph,
            placeholder="306234280, THANOS IS RIGHT, TC 10-2, 259\n123051289, SKORN, 80, 259",
            default=prefill,
            required=True,
            max_length=4000,
        )
        self.add_item(self.lines_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not await check_interaction_user(interaction, self.parent_view.author_id):
            return
        updated, skipped, errors, state_fids = await asyncio.to_thread(
            apply_edit_lines, self.lines_input.value, self.alliance_id)
        caught = enqueue_catchups(self.parent_view.cog.bot, state_fids)
        logger.info(f"Bulk member edit on alliance {self.alliance_id}: "
                    f"{updated} updated, {skipped} unchanged, {len(errors)} rejected")
        await interaction.response.send_message(
            embed=edit_result_embed(f"{theme.editListIcon} Bulk Edit Complete",
                                    updated, skipped, errors, caught=caught),
            ephemeral=True)
        await self.parent_view.refresh_after_edit(interaction, None, already_responded=True)


def build_prefill(members, limit=25):
    """Bulk-modal lines; placeholder names are blanked so the admin types into an empty slot."""
    lines = []
    for m in members[:limit]:
        name = "" if is_placeholder_name(m['nickname'], m['fid']) else m['nickname']
        level = format_furnace_level(m['furnace_lv']) if m.get('furnace_lv') else ""
        state = m['kid'] if m.get('kid') else ""
        lines.append(f"{m['fid']}, {name}, {level}, {state}")
    return "\n".join(lines)
