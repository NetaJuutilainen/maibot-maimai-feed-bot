"""投喂插件 — 管理员命令 Mixin。"""

from __future__ import annotations

from typing import Any

import re
import time

from maibot_sdk import Command

from .config import (
    FeedBotConfig,
    MAX_SIGN_BASE_POINTS,
    STYLE_CLEAR_ALL_LABEL,
    STYLE_CLEAR_ALL_MARKER,
)
from .db import AsyncDatabase
from .utils import is_likely_emoji

SET_SIGN_POINTS_COMMAND_PATTERN = r"^/投喂管理\s+签到积分(?:\s+(?P<value>.*))?$"


class AdminCommandsMixin:
    """管理员命令：全局商店、群商店、积分调整、属性设置、授权管理。"""

    # 这些属性由 FeedBotPlugin 提供，类型标注仅供静态分析
    db: AsyncDatabase
    config: FeedBotConfig
    _enabled_groups: set[str]

    # ---- 权限与过滤 ----

    def _is_bot_admin(self, user_id: str) -> bool:
        """判断是否为Bot管理员。"""
        return user_id in self.config.admin.admin_users

    async def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        """判断是否为指定群的管理员（含Bot管理员）。从数据库读取运行时授权。"""
        if self._is_bot_admin(user_id):
            return True
        admins = await self.db.get_group_admins(group_id)
        return user_id in admins

    def _group_whitelist_mode(self) -> bool:
        """是否处于「按群隔离/白名单」模式：config.toml 的 group_admins 即唯一开关。"""
        try:
            return bool(self.config.filter.group_admins_whitelist)
        except Exception:
            return True

    def _enabled_group_ids(self) -> set[str]:
        """返回当前已启用的群号。"""
        return set(self._enabled_groups)

    def _is_group_enabled(self, group_id: str) -> bool:
        """判断群是否已在数据库中启用。"""
        return group_id in self._enabled_groups

    def _check_group_enabled(self, group_id: str) -> bool:
        """群聊时检查群是否授权，私聊时放行。未授权群静默忽略。"""
        if group_id and not self._is_group_enabled(group_id):
            return False
        return True

    # ---- 道具参数解析 ----

    @staticmethod
    def _parse_item_rest(rest: str) -> tuple[float, str, str]:
        """从上架命令的剩余文本中提取饱食度、emoji 和描述。"""
        satiety_bonus = 5.0
        emoji = ""
        desc = ""
        if rest:
            satiety_match = re.search(r"饱食度(-?\d+(?:\.\d+)?)", rest)
            if satiety_match:
                satiety_bonus = float(satiety_match.group(1))
                rest = rest[:satiety_match.start()] + rest[satiety_match.end():]
                rest = rest.strip()
            if rest:
                tokens = rest.split(None, 1)
                first = tokens[0]
                if is_likely_emoji(first):
                    emoji = first
                    desc = tokens[1].strip() if len(tokens) > 1 else ""
                else:
                    desc = rest
        return satiety_bonus, emoji, desc

    # ---- Bot管理员命令 ----

    @Command(
        "admin_global_add_item",
        description="上架全局道具（Bot管理员）",
        pattern=r"^/投喂管理\s+全局上架\s+(?P<name>\S+)\s+(?P<price>\d+)(?:\s+(?P<rest>.+))?$",
    )
    async def handle_admin_global_add_item(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 全局上架 命令。"""
        if group_id:
            await self.ctx.send.text("Bot管理员命令请私聊Bot使用", stream_id)
            return False, "非私聊", True
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        name = str(matched_groups.get("name") or "").strip()
        price_str = str(matched_groups.get("price") or "").strip()
        rest = str(matched_groups.get("rest") or "").strip()

        if not name or not price_str:
            await self.ctx.send.text("用法：/投喂管理 全局上架 <名称> <价格> [emoji] [描述] [饱食度N]", stream_id)
            return False, "参数缺失", True

        try:
            price = int(price_str)
        except ValueError:
            await self.ctx.send.text("价格必须是整数", stream_id)
            return False, "价格格式错误", True

        satiety_bonus, emoji, desc = self._parse_item_rest(rest)

        row = await self.db.fetchone(
            "SELECT 1 FROM shop_items WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
            (name,),
        )
        if row:
            await self.ctx.send.text(f"全局道具「{name}」已存在", stream_id)
            return False, "道具已存在", True

        await self.db.execute_commit(
            """
            INSERT INTO shop_items (name, emoji, description, price, scope, group_id,
                                     is_on_sale, satiety_bonus, created_by, created_at)
            VALUES (?, ?, ?, ?, 'global', '', 1, ?, ?, ?)
            """,
            (name, emoji, desc, price, satiety_bonus, user_id, time.time()),
        )

        display = f"{emoji}{name}" if emoji else name
        await self.ctx.send.text(
            f"✅ 全局道具「{display}」已上架，价格：{price}积分，饱食度{satiety_bonus:+}", stream_id
        )
        return True, f"上架全局道具{name}", True

    @Command(
        "admin_global_remove_item",
        description="下架全局道具（Bot管理员）",
        pattern=r"^/投喂管理\s+全局下架\s+(?P<item_name>.+)$",
    )
    async def handle_admin_global_remove_item(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 全局下架 命令。"""
        if group_id:
            await self.ctx.send.text("Bot管理员命令请私聊Bot使用", stream_id)
            return False, "非私聊", True
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂管理 全局下架 <道具名>", stream_id)
            return False, "缺少道具名", True

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET is_on_sale = 0 WHERE name = ? AND scope = 'global'",
            (item_name,),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到全局道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(f"✅ 全局道具「{item_name}」已下架", stream_id)
        return True, f"下架全局道具{item_name}", True

    @Command(
        "admin_modify_satiety",
        description="修改道具饱食度（管理员）",
        pattern=r"^/投喂管理\s+修改饱食度\s+(?P<scope>全局|群)\s+(?P<item_name>.+?)\s+(?P<satiety>-?\d+(?:\.\d+)?)$",
    )
    async def handle_admin_modify_satiety(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 修改饱食度 命令。"""
        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        scope = str(matched_groups.get("scope") or "").strip()
        item_name = str(matched_groups.get("item_name") or "").strip()
        satiety_str = str(matched_groups.get("satiety") or "").strip()

        if not scope or not item_name or not satiety_str:
            await self.ctx.send.text("用法：/投喂管理 修改饱食度 <全局|群> <道具名> <饱食度>", stream_id)
            return False, "参数缺失", True

        try:
            satiety_bonus = float(satiety_str)
        except ValueError:
            await self.ctx.send.text("饱食度必须是数字", stream_id)
            return False, "饱食度格式错误", True

        if scope == "全局":
            rowcount = await self.db.execute_rowcount(
                "UPDATE shop_items SET satiety_bonus = ? WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
                (satiety_bonus, item_name),
            )
        else:
            # 群道具：需要群号
            effective_group = group_id
            if not effective_group:
                await self.ctx.send.text("修改群道具饱食度需要在群聊中使用", stream_id)
                return False, "缺少群号", True
            rowcount = await self.db.execute_rowcount(
                "UPDATE shop_items SET satiety_bonus = ? WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1",
                (satiety_bonus, item_name, effective_group),
            )

        if rowcount == 0:
            scope_label = "全局" if scope == "全局" else "群内"
            await self.ctx.send.text(f"未找到{scope_label}在售道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        scope_label = "全局" if scope == "全局" else "群内"
        await self.ctx.send.text(f"✅ {scope_label}道具「{item_name}」饱食度已修改为{satiety_bonus:+}", stream_id)
        return True, f"修改道具饱食度{item_name}", True

    @Command(
        "admin_set_group_item_style",
        description="设置群道具投喂后的临时说话风格（群管理员，在群内发送）",
        pattern=r"^/投喂管理\s+设置风格\s+(?P<item_name>\S+)\s+(?P<duration>\d+)\s+(?P<style_prompt>.+)$",
    )
    async def handle_admin_set_group_item_style(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 设置风格 命令：在哪个群发送就对哪个群的同名道具生效。"""
        if not group_id:
            await self.ctx.send.text("设置群道具风格需要在群聊中使用", stream_id)
            return False, "缺少群号", True
        if not await self._is_group_admin(group_id, user_id):
            await self.ctx.send.text("只有管理员才能设置群道具风格", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        item_name = str(matched_groups.get("item_name") or "").strip()
        duration_str = str(matched_groups.get("duration") or "").strip()
        style_prompt = str(matched_groups.get("style_prompt") or "").strip()

        if not item_name or not duration_str or not style_prompt:
            await self.ctx.send.text(
                "用法：/投喂管理 设置风格 <道具名> <时长分钟> <风格描述>", stream_id
            )
            return False, "参数缺失", True

        try:
            duration = int(duration_str)
        except ValueError:
            await self.ctx.send.text("时长必须是整数（分钟）", stream_id)
            return False, "时长格式错误", True
        if duration < 1:
            await self.ctx.send.text("时长必须是 ≥1 的整数（分钟）", stream_id)
            return False, "时长过小", True
        if len(style_prompt) > 500:
            await self.ctx.send.text("风格描述过长（最多 500 字）", stream_id)
            return False, "描述过长", True

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET style_prompt = ?, style_duration_minutes = ? "
            "WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1",
            (style_prompt, duration, item_name, group_id),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到本群在售道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(
            f"✅ 本群道具「{item_name}」已设置投喂风格（{duration} 分钟）：{style_prompt}",
            stream_id,
        )
        return True, f"设置道具风格{item_name}", True

    @Command(
        "admin_clear_group_item_style",
        description="清除群道具投喂后的临时说话风格（群管理员，在群内发送）",
        pattern=r"^/投喂管理\s+清除风格\s+(?P<item_name>\S+)$",
    )
    async def handle_admin_clear_group_item_style(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 清除风格 命令：清除当前群同名道具的风格。"""
        if not group_id:
            await self.ctx.send.text("清除群道具风格需要在群聊中使用", stream_id)
            return False, "缺少群号", True
        if not await self._is_group_admin(group_id, user_id):
            await self.ctx.send.text("只有管理员才能清除群道具风格", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂管理 清除风格 <道具名>", stream_id)
            return False, "参数缺失", True

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET style_prompt = '', style_duration_minutes = 0 "
            "WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1",
            (item_name, group_id),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到本群在售道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(f"✅ 已清除本群道具「{item_name}」的投喂风格", stream_id)
        return True, f"清除道具风格{item_name}", True

    @Command(
        "admin_set_global_item_style",
        description="设置全局道具投喂后的临时说话风格（Bot管理员）",
        pattern=r"^/投喂管理\s+全局设置风格\s+(?P<item_name>\S+)\s+(?P<duration>\d+)\s+(?P<style_prompt>.+)$",
    )
    async def handle_admin_set_global_item_style(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 全局设置风格 命令。"""
        if group_id:
            await self.ctx.send.text("Bot管理员命令请私聊Bot使用", stream_id)
            return False, "非私聊", True
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能设置全局道具风格", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        item_name = str(matched_groups.get("item_name") or "").strip()
        duration_str = str(matched_groups.get("duration") or "").strip()
        style_prompt = str(matched_groups.get("style_prompt") or "").strip()

        if not item_name or not duration_str or not style_prompt:
            await self.ctx.send.text(
                "用法：/投喂管理 全局设置风格 <道具名> <时长分钟> <风格描述>", stream_id
            )
            return False, "参数缺失", True

        try:
            duration = int(duration_str)
        except ValueError:
            await self.ctx.send.text("时长必须是整数（分钟）", stream_id)
            return False, "时长格式错误", True
        if duration < 1:
            await self.ctx.send.text("时长必须是 ≥1 的整数（分钟）", stream_id)
            return False, "时长过小", True
        if len(style_prompt) > 500:
            await self.ctx.send.text("风格描述过长（最多 500 字）", stream_id)
            return False, "描述过长", True

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET style_prompt = ?, style_duration_minutes = ? "
            "WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
            (style_prompt, duration, item_name),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到全局在售道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(
            f"✅ 全局道具「{item_name}」已设置投喂风格（{duration} 分钟）：{style_prompt}",
            stream_id,
        )
        return True, f"设置全局道具风格{item_name}", True

    @Command(
        "admin_clear_global_item_style",
        description="清除全局道具投喂后的临时说话风格（Bot管理员）",
        pattern=r"^/投喂管理\s+全局清除风格\s+(?P<item_name>\S+)$",
    )
    async def handle_admin_clear_global_item_style(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 全局清除风格 命令。"""
        if group_id:
            await self.ctx.send.text("Bot管理员命令请私聊Bot使用", stream_id)
            return False, "非私聊", True
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能清除全局道具风格", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂管理 全局清除风格 <道具名>", stream_id)
            return False, "参数缺失", True

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET style_prompt = '', style_duration_minutes = 0 "
            "WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
            (item_name,),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到全局在售道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(f"✅ 已清除全局道具「{item_name}」的投喂风格", stream_id)
        return True, f"清除全局道具风格{item_name}", True

    @Command(
        "admin_set_group_clear_effect",
        description="把群道具设为清除型道具：吃下清空该群全部投喂风格（群管理员，在群内发送）",
        pattern=r"^/投喂管理\s+设置清除效果\s+(?P<item_name>\S+)$",
    )
    async def handle_admin_set_group_clear_effect(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 设置清除效果 命令：把本群同名道具设为清除型。"""
        if not group_id:
            await self.ctx.send.text("设置群道具清除效果需要在群聊中使用", stream_id)
            return False, "缺少群号", True
        if not await self._is_group_admin(group_id, user_id):
            await self.ctx.send.text("只有管理员才能设置群道具清除效果", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂管理 设置清除效果 <道具名>", stream_id)
            return False, "参数缺失", True

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET style_prompt = ?, style_duration_minutes = 0 "
            "WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1",
            (STYLE_CLEAR_ALL_MARKER, item_name, group_id),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到本群在售道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(
            f"✅ 本群道具「{item_name}」已设为清除型道具（吃下将{STYLE_CLEAR_ALL_LABEL}）",
            stream_id,
        )
        return True, f"设置群道具清除效果{item_name}", True

    @Command(
        "admin_set_global_clear_effect",
        description="把全局道具设为清除型道具：吃下清空该群全部投喂风格（Bot管理员）",
        pattern=r"^/投喂管理\s+全局设置清除效果\s+(?P<item_name>\S+)$",
    )
    async def handle_admin_set_global_clear_effect(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 全局设置清除效果 命令。"""
        if group_id:
            await self.ctx.send.text("Bot管理员命令请私聊Bot使用", stream_id)
            return False, "非私聊", True
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能设置全局道具清除效果", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂管理 全局设置清除效果 <道具名>", stream_id)
            return False, "参数缺失", True

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET style_prompt = ?, style_duration_minutes = 0 "
            "WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
            (STYLE_CLEAR_ALL_MARKER, item_name),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到全局在售道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(
            f"✅ 全局道具「{item_name}」已设为清除型道具（吃下将{STYLE_CLEAR_ALL_LABEL}）",
            stream_id,
        )
        return True, f"设置全局道具清除效果{item_name}", True

    @Command(
        "admin_points",
        description="设置用户积分（覆盖式，管理员）",
        pattern=r"^/投喂管理\s+积分\s+(?P<target_user>\S+)\s+(?P<amount>-?\d+)(?:\s+(?P<target_group>\S+))?$",
    )
    async def handle_admin_points(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 积分 命令。"""
        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_user = str(matched_groups.get("target_user") or "").strip()
        amount_str = str(matched_groups.get("amount") or "").strip()
        target_group = str(matched_groups.get("target_group") or "").strip()

        if not target_user or not amount_str:
            await self.ctx.send.text("用法：/投喂管理 积分 <QQ号> <数量> [群号]", stream_id)
            return False, "参数缺失", True

        effective_group = target_group or group_id
        if not effective_group:
            await self.ctx.send.text("需要指定群号：/投喂管理 积分 <QQ号> <数量> <群号>", stream_id)
            return False, "缺少群号", True

        try:
            amount = int(amount_str)
        except ValueError:
            await self.ctx.send.text("数量必须是整数", stream_id)
            return False, "数量格式错误", True

        uid = self.db.user_key(target_user, effective_group)
        await self.db.ensure_user(target_user, "", effective_group)

        # 覆盖式设置：直接写入目标积分（负数按 0 处理），不再累加
        new_points = max(0, amount)
        await self.db.execute_commit(
            "UPDATE users SET points = ? WHERE user_id = ?",
            (new_points, uid),
        )

        await self.ctx.send.text(
            f"✅ 已将 {target_user} 在群 {effective_group} 的积分设置为 {new_points}（覆盖）",
            stream_id,
        )
        return True, f"设置积分{new_points}", True

    @Command(
        "admin_attr",
        description="设置Bot属性（管理员）",
        pattern=r"^/投喂管理\s+属性\s+(?P<attr_key>\S+)\s+(?P<attr_value>\d+\.?\d*)(?:\s+(?P<target_group>\S+))?$",
    )
    async def handle_admin_attr(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 属性 命令。"""
        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        attr_key = str(matched_groups.get("attr_key") or "").strip()
        attr_value_str = str(matched_groups.get("attr_value") or "").strip()
        target_group = str(matched_groups.get("target_group") or "").strip()

        if not attr_key or not attr_value_str:
            await self.ctx.send.text(
                "用法：/投喂管理 属性 <属性名> <值> [群号]（属性名支持 satiety / 饱食度）",
                stream_id,
            )
            return False, "参数缺失", True

        if attr_key not in ("satiety", "饱食度"):
            await self.ctx.send.text(
                "无效属性名，当前仅支持：satiety / 饱食度", stream_id
            )
            return False, "无效属性名", True

        effective_group = target_group or group_id
        if not effective_group:
            await self.ctx.send.text("需要指定群号：/投喂管理 属性 satiety <值> <群号>", stream_id)
            return False, "缺少群号", True

        try:
            attr_value = float(attr_value_str)
        except ValueError:
            await self.ctx.send.text("属性值必须是数字", stream_id)
            return False, "属性值格式错误", True

        await self.db.set_satiety(attr_value, effective_group)

        await self.ctx.send.text(
            f"✅ 群 {effective_group} 饱食度已设置为 {attr_value:.0f}", stream_id
        )
        return True, f"设置属性{attr_key}", True

    @Command(
        "admin_reset_sign",
        description="重置用户签到（管理员）",
        pattern=r"^/投喂管理\s+重置签到\s+(?P<target_user>\S+)(?:\s+(?P<target_group>\S+))?$",
    )
    async def handle_admin_reset_sign(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 重置签到 命令。"""
        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_user = str(matched_groups.get("target_user") or "").strip()
        target_group = str(matched_groups.get("target_group") or "").strip()

        if not target_user:
            await self.ctx.send.text("用法：/投喂管理 重置签到 <QQ号> [群号]", stream_id)
            return False, "参数缺失", True

        effective_group = target_group or group_id
        if not effective_group:
            await self.ctx.send.text("需要指定群号：/投喂管理 重置签到 <QQ号> <群号>", stream_id)
            return False, "缺少群号", True

        uid = self.db.user_key(target_user, effective_group)
        await self.db.execute_commit(
            """
            UPDATE users SET total_sign_days = 0, consecutive_sign_days = 0, last_sign_time = 0
            WHERE user_id = ?
            """,
            (uid,),
        )

        await self.ctx.send.text(f"✅ 已重置 {target_user} 的签到记录", stream_id)
        return True, f"重置签到{target_user}", True

    @Command(
        "admin_set_sign_points",
        description="设置全局签到基础积分（Bot管理员）",
        pattern=SET_SIGN_POINTS_COMMAND_PATTERN,
    )
    async def handle_admin_set_sign_points(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 签到积分 命令，设置全局签到基础积分。"""
        if group_id:
            await self.ctx.send.text("Bot管理员命令请私聊Bot使用", stream_id)
            return False, "非私聊", True
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        value_str = str(matched_groups.get("value") or "").strip()
        if not value_str:
            await self.ctx.send.text("用法：/投喂管理 签到积分 <数值>", stream_id)
            return False, "参数缺失", True

        try:
            value = int(value_str)
        except ValueError:
            await self.ctx.send.text("数值必须是整数", stream_id)
            return False, "数值格式错误", True

        if not 0 <= value <= MAX_SIGN_BASE_POINTS:
            await self.ctx.send.text(
                f"签到基础积分必须在 0-{MAX_SIGN_BASE_POINTS:,} 之间", stream_id
            )
            return False, "数值超出范围", True

        # 命令设置是独立于 config.toml 的全局持久化覆盖值
        await self.db.set_setting("sign_base_points", str(value))

        await self.ctx.send.text(f"✅ 签到基础积分已设置为 {value}", stream_id)
        return True, f"设置签到积分{value}", True

    @Command(
        "admin_grant",
        description="授权群管理员（管理员）",
        pattern=r"^/投喂管理\s+授权\s+(?P<group_id>\S+)\s+(?P<target_user>\S+)$",
    )
    async def handle_admin_grant(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 授权 命令。"""
        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_group_id = str(matched_groups.get("group_id") or "").strip()
        target_user = str(matched_groups.get("target_user") or "").strip()

        if not target_group_id or not target_user:
            await self.ctx.send.text("用法：/投喂管理 授权 <群号> <QQ号>", stream_id)
            return False, "参数缺失", True

        if group_id and target_group_id != group_id:
            await self.ctx.send.text("群聊中只能授权当前群的管理员", stream_id)
            return False, "跨群授权", True

        # 写入数据库（运行时授权）。白名单模式下只加管理员、不改变群的启用状态，
        # 避免「加管理员」意外把未列入 config.toml 的群打开。
        whitelist_mode = self._group_whitelist_mode()
        await self.db.add_group_admin(
            target_group_id, target_user, enable=not whitelist_mode
        )
        if whitelist_mode:
            if self._is_group_enabled(target_group_id):
                await self.ctx.send.text(
                    f"✅ 已授权 {target_user} 为群 {target_group_id} 的管理员", stream_id
                )
            else:
                await self.ctx.send.text(
                    f"✅ 已记录 {target_user} 为群 {target_group_id} 的管理员，"
                    f"但该群未开启投喂插件。\n"
                    f"当前为按群隔离模式，需要在 config.toml 的 "
                    f"[[filter.group_admins]] 里加上群号 {target_group_id} 才会启用",
                    stream_id,
                )
            return True, f"授权群管理员{target_user}", True

        self._enabled_groups.add(target_group_id)

        await self.ctx.send.text(
            f"✅ 已授权 {target_user} 为群 {target_group_id} 的管理员", stream_id
        )
        return True, f"授权群管理员{target_user}", True

    @Command(
        "admin_revoke",
        description="取消群管理员授权（管理员）",
        pattern=r"^/投喂管理\s+取消授权\s+(?P<group_id>\S+)\s+(?P<target_user>\S+)$",
    )
    async def handle_admin_revoke(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 取消授权 命令。"""
        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_group_id = str(matched_groups.get("group_id") or "").strip()
        target_user = str(matched_groups.get("target_user") or "").strip()

        if not target_group_id or not target_user:
            await self.ctx.send.text("用法：/投喂管理 取消授权 <群号> <QQ号>", stream_id)
            return False, "参数缺失", True

        if group_id and target_group_id != group_id:
            await self.ctx.send.text("群聊中只能取消授权当前群的管理员", stream_id)
            return False, "跨群取消授权", True

        # 不允许取消 Bot 管理员的授权
        if self._is_bot_admin(target_user):
            await self.ctx.send.text("无法取消Bot管理员的授权", stream_id)
            return False, "无法取消Bot管理员", True

        # 从数据库移除（运行时授权）
        await self.db.remove_group_admin(target_group_id, target_user)

        await self.ctx.send.text(
            f"✅ 已取消 {target_user} 在群 {target_group_id} 的管理员权限", stream_id
        )
        return True, f"取消授权{target_user}", True

    @Command(
        "admin_list_groups",
        description="查看已授权的群及管理员（管理员）",
        pattern=r"^/投喂管理\s+群列表$",
    )
    async def handle_admin_list_groups(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 群列表 命令，显示所有已配置群及其管理员。"""
        del kwargs

        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        # 从数据库获取已启用的群列表
        enabled_groups = self._enabled_group_ids()
        if not enabled_groups:
            await self.ctx.send.text("暂无已授权的群", stream_id)
            return True, "无授权群", True

        lines = ["📋 已授权群列表"]
        for gid in sorted(enabled_groups):
            admins = await self.db.get_group_admins(gid)
            if admins:
                admin_str = ", ".join(admins)
            else:
                admin_str = "（无管理员）"
            lines.append(f"  群 {gid}：{admin_str}")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "群列表", True

    # ---- 群管理员命令 ----

    @Command(
        "admin_group_add_item",
        description="上架群内道具（群管理员）",
        pattern=r"^/投喂管理\s+群上架\s+(?P<name>\S+)\s+(?P<price>\d+)(?:\s+(?P<rest>.+))?$",
    )
    async def handle_admin_group_add_item(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 群上架 命令。"""
        if not group_id:
            await self.ctx.send.text("群上架仅支持在群聊中使用", stream_id)
            return False, "非群聊", True

        if not await self._is_group_admin(group_id, user_id):
            await self.ctx.send.text("只有群管理员或Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        name = str(matched_groups.get("name") or "").strip()
        price_str = str(matched_groups.get("price") or "").strip()
        rest = str(matched_groups.get("rest") or "").strip()

        if not name or not price_str:
            await self.ctx.send.text("用法：/投喂管理 群上架 <名称> <价格> [emoji] [描述] [饱食度N]", stream_id)
            return False, "参数缺失", True

        try:
            price = int(price_str)
        except ValueError:
            await self.ctx.send.text("价格必须是整数", stream_id)
            return False, "价格格式错误", True

        satiety_bonus, emoji, desc = self._parse_item_rest(rest)

        row = await self.db.fetchone(
            "SELECT 1 FROM shop_items WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1",
            (name, group_id),
        )
        if row:
            await self.ctx.send.text(f"本群道具「{name}」已存在", stream_id)
            return False, "道具已存在", True

        await self.db.execute_commit(
            """
            INSERT INTO shop_items (name, emoji, description, price, scope, group_id,
                                     is_on_sale, satiety_bonus, created_by, created_at)
            VALUES (?, ?, ?, ?, 'group', ?, 1, ?, ?, ?)
            """,
            (name, emoji, desc, price, group_id, satiety_bonus, user_id, time.time()),
        )

        display = f"{emoji}{name}" if emoji else name
        await self.ctx.send.text(
            f"✅ 本群道具「{display}」已上架，价格：{price}积分，饱食度{satiety_bonus:+}", stream_id
        )
        return True, f"群上架{name}", True

    @Command(
        "admin_group_remove_item",
        description="下架群内道具（群管理员）",
        pattern=r"^/投喂管理\s+群下架\s+(?P<item_name>.+)$",
    )
    async def handle_admin_group_remove_item(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 群下架 命令。"""
        if not group_id:
            await self.ctx.send.text("群下架仅支持在群聊中使用", stream_id)
            return False, "非群聊", True

        if not await self._is_group_admin(group_id, user_id):
            await self.ctx.send.text("只有群管理员或Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂管理 群下架 <道具名>", stream_id)
            return False, "缺少道具名", True

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET is_on_sale = 0 WHERE name = ? AND scope = 'group' AND group_id = ?",
            (item_name, group_id),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到本群道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(f"✅ 本群道具「{item_name}」已下架", stream_id)
        return True, f"群下架{item_name}", True

    @Command(
        "admin_help",
        description="查看管理员可用指令列表",
        pattern=r"^/投喂管理\s+规则$",
    )
    async def handle_admin_help(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 规则 命令：输出管理员可用的全部指令。"""
        del kwargs

        if not user_id:
            return False, "无法获取用户信息", True

        # 管理员门槛：Bot管理员，或群聊中的群管理员
        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能查看管理规则", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能查看管理规则", stream_id)
            return False, "非管理员", True

        lines = [
            "📖 投喂管理规则（管理员指令）",
            "",
            "【Bot管理员 · 私聊】",
            "/投喂管理 全局上架 <名称> <价格> [emoji] [描述] [饱食度N]",
            "/投喂管理 全局下架 <道具名>",
            "/投喂管理 全局设置风格 <道具名> <时长分钟> <风格描述>",
            "/投喂管理 全局清除风格 <道具名>",
            "/投喂管理 全局设置清除效果 <道具名>",
            "/投喂管理 签到积分 <数值>",
            "",
            "【群管理员 · 群内】",
            "/投喂管理 群上架 <名称> <价格> [emoji] [描述] [饱食度N]",
            "/投喂管理 群下架 <道具名>",
            "/投喂管理 设置风格 <道具名> <时长分钟> <风格描述>",
            "/投喂管理 清除风格 <道具名>",
            "/投喂管理 设置清除效果 <道具名>",
            "",
            "【群管理员·群内 / Bot管理员·私聊】",
            "/投喂管理 修改饱食度 <全局|群> <道具名> <饱食度>",
            "/投喂管理 积分 <QQ号> <积分值> [群号]（覆盖式设置，不是增减）",
            "/投喂管理 属性 <属性名> <值> [群号]",
            "/投喂管理 重置签到 <QQ号> [群号]",
            "/投喂管理 授权 <群号> <QQ号>",
            "/投喂管理 取消授权 <群号> <QQ号>",
            "/投喂管理 群列表",
        ]

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "投喂管理规则", True
