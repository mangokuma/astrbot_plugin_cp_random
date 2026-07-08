import glob

import json
import os
import random
import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from io import BytesIO

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

import astrbot.api.message_components as Comp


class CpRandomPlugin(Star):
    """
    AstrBot 群友 CP 随机抽取插件
    支持随机老公/老婆、换老公/老婆、关系图生成、绑定模式与非绑定模式
    """

    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = os.path.join("data", "plugins", "astrbot_plugin_cp_random")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_file = os.path.join(self.data_dir, "cp_data.json")
        self.data = self._load_data()
        self.lock = asyncio.Lock()
        # 启动定时重置任务
        asyncio.create_task(self._daily_reset_task())
        logger.info("群友 CP 随机抽取插件已加载")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为群主或管理员，尝试多种路径获取 role"""
        role = None
        msg_obj = getattr(event, 'message_obj', None)
        if msg_obj is not None:
            sender = getattr(msg_obj, 'sender', None)
            if sender is not None:
                if isinstance(sender, dict):
                    role = sender.get('role')
                else:
                    role = getattr(sender, 'role', None)
            # 尝试 raw_message 路径
            if role is None:
                raw = getattr(msg_obj, 'raw_message', None)
                if isinstance(raw, dict):
                    s = raw.get('sender', {})
                    if isinstance(s, dict):
                        role = s.get('role')
        # 备用路径
        if role is None:
            platform_msg = getattr(event, 'platform_message', None)
            if platform_msg is not None:
                sender = getattr(platform_msg, 'sender', None)
                if isinstance(sender, dict):
                    role = sender.get('role')
                elif sender is not None:
                    role = getattr(sender, 'role', None)
        return role in ('owner', 'admin')

    def _load_data(self) -> Dict[str, Any]:
        """加载持久化数据"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载数据失败: {e}")
        return {"groups": {}}

    def _save_data(self):
        """保存持久化数据"""
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存数据失败: {e}")

    def _get_group_data(self, group_id: str) -> Dict[str, Any]:
        """获取指定群的数据，不存在则初始化"""
        if group_id not in self.data["groups"]:
            self.data["groups"][group_id] = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "mode": "random",  # 默认非绑定模式
                "max_swaps": 1,
                "husbands": {},  # {user_id: target_id}
                "wives": {},     # {user_id: target_id}
                "husband_swaps": {},  # {user_id: remaining}
                "wife_swaps": {},     # {user_id: remaining}
                "members": {},   # 缓存群成员信息 {user_id: {nickname, card, avatar}}
            }
        return self.data["groups"][group_id]

    def _check_and_reset(self, group_id: str):
        """检查并执行每日重置"""
        group_data = self._get_group_data(group_id)
        today = datetime.now().strftime("%Y-%m-%d")
        if group_data.get("date") != today:
            group_data["date"] = today
            group_data["husbands"] = {}
            group_data["wives"] = {}
            group_data["husband_swaps"] = {}
            group_data["wife_swaps"] = {}
            self._save_data()
            logger.info(f"群 {group_id} 已执行每日重置")

    async def _daily_reset_task(self):
        """定时任务：每天 00:00 重置所有群数据"""
        while True:
            now = datetime.now()
            # 计算到下一个 00:00 的时间
            next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            wait_seconds = (next_midnight - now).total_seconds()
            logger.info(f"距离下次每日重置还有 {wait_seconds:.0f} 秒")
            await asyncio.sleep(wait_seconds)
            # 重置所有群数据
            for group_id in list(self.data["groups"].keys()):
                self._check_and_reset(group_id)
            # 清理关系图缓存
            try:
                for f in glob.glob(os.path.join(self.data_dir, "graph_*.png")):
                    os.remove(f)
                    logger.info(f"已清理关系图缓存: {f}")
            except Exception as e:
                logger.warning(f"清理关系图缓存失败: {e}")
            logger.info("所有群数据已执行每日重置，关系图缓存已清理")

    async def _get_group_members(self, event: AiocqhttpMessageEvent) -> List[Dict[str, Any]]:
        """获取群成员列表"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                return []
            client = event.bot
            params = {"group_id": group_id}
            result = await client.api.call_action('get_group_member_list', **params)
            if isinstance(result, list):
                return result
            return []
        except Exception as e:
            logger.error(f"获取群成员列表失败: {e}")
            return []

    def _get_member_info(self, members: List[Dict], user_id: str) -> Optional[Dict]:
        """从成员列表中获取指定用户信息"""
        for member in members:
            uid = str(member.get("user_id", ""))
            if uid == str(user_id):
                return {
                    "user_id": uid,
                    "nickname": member.get("nickname", "未知"),
                    "card": member.get("card", ""),
                    "display_name": member.get("card") or member.get("nickname") or f"用户{uid}",
                }
        return None

    def _get_qq_avatar_url(self, user_id: str) -> str:
        """获取 QQ 头像 URL"""
        return f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"

    def _get_candidates(self, members: List[Dict], exclude_id: str) -> List[Dict]:
        """获取候选成员列表（排除指定用户）"""
        return [
            {
                "user_id": str(m.get("user_id", "")),
                "nickname": m.get("nickname", "未知"),
                "card": m.get("card", ""),
                "display_name": m.get("card") or m.get("nickname") or f"用户{m.get('user_id', '')}",
            }
            for m in members
            if str(m.get("user_id", "")) != str(exclude_id)
        ]

    def _find_binding_partner(self, group_data: Dict, user_id: str, relation_type: str) -> Optional[str]:
        """
        在绑定模式下查找配对对象
        relation_type: 'husband' 表示用户要抽老公，查找 wives 中 value == user_id 的 key
                      'wife' 表示用户要抽老婆，查找 husbands 中 value == user_id 的 key
        """
        if group_data.get("mode") != "bind":
            return None
        
        if relation_type == "husband":
            # 用户要抽老公：找谁把 user_id 当老婆（wives 中 value == user_id）
            # 那人的老婆是 user_id → 所以 user_id 的老公 = 那人
            for uid, wife_id in group_data.get("wives", {}).items():
                if str(wife_id) == str(user_id):
                    return str(uid)
        elif relation_type == "wife":
            # 用户要抽老婆：找谁把 user_id 当老公（husbands 中 value == user_id）
            # 那人的老公是 user_id → 所以 user_id 的老婆 = 那人
            for uid, husband_id in group_data.get("husbands", {}).items():
                if str(husband_id) == str(user_id):
                    return str(uid)
        return None

    # ========== 指令处理 ==========

    @filter.command("随机老公")
    async def random_husband(self, event: AstrMessageEvent):
        '''随机抽取一位群成员作为你的老公'''
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("此插件仅支持 aiocqhttp (QQ) 平台")
            return
        
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return
        
        user_id = str(event.get_sender_id())
        
        async with self.lock:
            self._check_and_reset(group_id)
            group_data = self._get_group_data(group_id)
            
            # 检查是否已有老公
            if user_id in group_data.get("husbands", {}):
                target_id = group_data["husbands"][user_id]
                members = await self._get_group_members(event)
                target_info = self._get_member_info(members, target_id)
                if target_info:
                    display_name = target_info["display_name"]
                    result = event.make_result()
                    result.chain = [
                        Comp.Plain(f"你今天已经有老公啦！\n你的老公是 {display_name}（{target_id}）~"),
                        Comp.Image(file=self._get_qq_avatar_url(target_id))
                    ]
                    yield result
                else:
                    yield event.plain_result(f"你今天已经有老公啦！老公QQ: {target_id}")
                return
            
            # 获取群成员
            members = await self._get_group_members(event)
            if not members:
                yield event.plain_result("获取群成员列表失败，请检查机器人权限")
                return
            
            candidates = self._get_candidates(members, user_id)
            if not candidates:
                yield event.plain_result("群里没有其他人可以抽取啦~")
                return
            
            # 绑定模式：检查是否有人把 user_id 当老婆
            partner_id = self._find_binding_partner(group_data, user_id, "husband")
            if partner_id:
                # 检查 partner 是否在候选中
                partner_info = self._get_member_info(members, partner_id)
                if partner_info:
                    target_id = partner_id
                else:
                    target_id = random.choice(candidates)["user_id"]
            else:
                target_id = random.choice(candidates)["user_id"]
            
            # 保存结果
            group_data["husbands"][user_id] = target_id
            group_data["husband_swaps"][user_id] = group_data.get("max_swaps", 1)
            self._save_data()
        
        # 获取目标信息并回复
        target_info = self._get_member_info(members, target_id)
        display_name = target_info["display_name"] if target_info else f"用户{target_id}"
        
        result = event.make_result()
        result.chain = [
            Comp.Plain(f"恭喜 {event.get_sender_name()}，你的老公是 {display_name}（{target_id}）~"),
            Comp.Image(file=self._get_qq_avatar_url(target_id))
        ]
        yield result

    @filter.command("随机老婆")
    async def random_wife(self, event: AstrMessageEvent):
        '''随机抽取一位群成员作为你的老婆'''
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("此插件仅支持 aiocqhttp (QQ) 平台")
            return
        
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return
        
        user_id = str(event.get_sender_id())
        
        async with self.lock:
            self._check_and_reset(group_id)
            group_data = self._get_group_data(group_id)
            
            # 检查是否已有老婆
            if user_id in group_data.get("wives", {}):
                target_id = group_data["wives"][user_id]
                members = await self._get_group_members(event)
                target_info = self._get_member_info(members, target_id)
                if target_info:
                    display_name = target_info["display_name"]
                    result = event.make_result()
                    result.chain = [
                        Comp.Plain(f"你今天已经有老婆啦！\n你的老婆是 {display_name}（{target_id}）~"),
                        Comp.Image(file=self._get_qq_avatar_url(target_id))
                    ]
                    yield result
                else:
                    yield event.plain_result(f"你今天已经有老婆啦！老婆QQ: {target_id}")
                return
            
            # 获取群成员
            members = await self._get_group_members(event)
            if not members:
                yield event.plain_result("获取群成员列表失败，请检查机器人权限")
                return
            
            candidates = self._get_candidates(members, user_id)
            if not candidates:
                yield event.plain_result("群里没有其他人可以抽取啦~")
                return
            
            # 绑定模式：检查是否有人把 user_id 当老公
            partner_id = self._find_binding_partner(group_data, user_id, "wife")
            if partner_id:
                partner_info = self._get_member_info(members, partner_id)
                if partner_info:
                    target_id = partner_id
                else:
                    target_id = random.choice(candidates)["user_id"]
            else:
                target_id = random.choice(candidates)["user_id"]
            
            # 保存结果
            group_data["wives"][user_id] = target_id
            group_data["wife_swaps"][user_id] = group_data.get("max_swaps", 1)
            self._save_data()
        
        target_info = self._get_member_info(members, target_id)
        display_name = target_info["display_name"] if target_info else f"用户{target_id}"
        
        result = event.make_result()
        result.chain = [
            Comp.Plain(f"恭喜 {event.get_sender_name()}，你的老婆是 {display_name}（{target_id}）~"),
            Comp.Image(file=self._get_qq_avatar_url(target_id))
        ]
        yield result

    @filter.command("换老公")
    async def swap_husband(self, event: AstrMessageEvent):
        '''重新随机抽取一位老公（消耗更换次数）'''
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("此插件仅支持 aiocqhttp (QQ) 平台")
            return
        
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return
        
        user_id = str(event.get_sender_id())
        
        async with self.lock:
            self._check_and_reset(group_id)
            group_data = self._get_group_data(group_id)
            
            # 检查是否有老公
            if user_id not in group_data.get("husbands", {}):
                yield event.plain_result("你还没有老公呢，先使用「随机老公」抽取一个吧~")
                return
            
            # 检查更换次数
            remaining = group_data.get("husband_swaps", {}).get(user_id, 0)
            if remaining <= 0:
                yield event.plain_result("渣男，你今天没有老公了！")
                return
            
            # 获取群成员
            members = await self._get_group_members(event)
            if not members:
                yield event.plain_result("获取群成员列表失败，请检查机器人权限")
                return
            
            current_husband = group_data["husbands"][user_id]
            candidates = self._get_candidates(members, user_id)
            # 排除当前老公
            candidates = [c for c in candidates if c["user_id"] != str(current_husband)]
            
            if not candidates:
                yield event.plain_result("群里没有其他人可以换啦~")
                return
            
            target_id = random.choice(candidates)["user_id"]
            group_data["husbands"][user_id] = target_id
            group_data["husband_swaps"][user_id] = remaining - 1
            self._save_data()
        
        target_info = self._get_member_info(members, target_id)
        display_name = target_info["display_name"] if target_info else f"用户{target_id}"
        
        result = event.make_result()
        result.chain = [
            Comp.Plain(f"换老公成功！\n恭喜 {event.get_sender_name()}，你的新老公是 {display_name}（{target_id}）~\n（剩余更换次数：{remaining - 1}）"),
            Comp.Image(file=self._get_qq_avatar_url(target_id))
        ]
        yield result

    @filter.command("换老婆")
    async def swap_wife(self, event: AstrMessageEvent):
        '''重新随机抽取一位老婆（消耗更换次数）'''
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("此插件仅支持 aiocqhttp (QQ) 平台")
            return
        
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return
        
        user_id = str(event.get_sender_id())
        
        async with self.lock:
            self._check_and_reset(group_id)
            group_data = self._get_group_data(group_id)
            
            # 检查是否有老婆
            if user_id not in group_data.get("wives", {}):
                yield event.plain_result("你还没有老婆呢，先使用「随机老婆」抽取一个吧~")
                return
            
            # 检查更换次数
            remaining = group_data.get("wife_swaps", {}).get(user_id, 0)
            if remaining <= 0:
                yield event.plain_result("渣男，你今天没有老婆了！")
                return
            
            # 获取群成员
            members = await self._get_group_members(event)
            if not members:
                yield event.plain_result("获取群成员列表失败，请检查机器人权限")
                return
            
            current_wife = group_data["wives"][user_id]
            candidates = self._get_candidates(members, user_id)
            candidates = [c for c in candidates if c["user_id"] != str(current_wife)]
            
            if not candidates:
                yield event.plain_result("群里没有其他人可以换啦~")
                return
            
            target_id = random.choice(candidates)["user_id"]
            group_data["wives"][user_id] = target_id
            group_data["wife_swaps"][user_id] = remaining - 1
            self._save_data()
        
        target_info = self._get_member_info(members, target_id)
        display_name = target_info["display_name"] if target_info else f"用户{target_id}"
        
        result = event.make_result()
        result.chain = [
            Comp.Plain(f"换老婆成功！\n恭喜 {event.get_sender_name()}，你的新老婆是 {display_name}（{target_id}）~\n（剩余更换次数：{remaining - 1}）"),
            Comp.Image(file=self._get_qq_avatar_url(target_id))
        ]
        yield result

    # ========== 配置指令 ==========

    @filter.command_group("CP设置")
    def cp_config(self):
        '''CP插件配置指令组'''
        pass

    @cp_config.command("模式")
    async def set_mode(self, event: AstrMessageEvent, mode: str):
        '''设置抽取模式：绑定模式(bind)或非绑定模式(random)'''
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return
        
        # 检查管理员权限
        if not self._is_admin(event):
            yield event.plain_result("只有群主或管理员才能使用此指令~")
            return
        
        mode = mode.lower().strip()
        if mode not in ("bind", "random"):
            yield event.plain_result("模式只能是 bind（绑定模式）或 random（非绑定模式）")
            return
        
        async with self.lock:
            group_data = self._get_group_data(group_id)
            group_data["mode"] = mode
            self._save_data()
        
        mode_text = "绑定模式" if mode == "bind" else "非绑定模式"
        yield event.plain_result(f"抽取模式已设置为：{mode_text}")

    @cp_config.command("次数")
    async def set_swaps(self, event: AstrMessageEvent, count: int):
        '''设置每天更换次数（1-10）'''
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return
        
        # 检查管理员权限
        if not self._is_admin(event):
            yield event.plain_result("只有群主或管理员才能使用此指令~")
            return
        
        if not 1 <= count <= 10:
            yield event.plain_result("更换次数必须在 1-10 之间")
            return
        
        async with self.lock:
            group_data = self._get_group_data(group_id)
            group_data["max_swaps"] = count
            self._save_data()
        
        yield event.plain_result(f"每天更换次数已设置为：{count} 次\n（新设置将在明天生效，或重置后生效）")

    @cp_config.command("状态")
    async def show_status(self, event: AstrMessageEvent):
        '''查看当前群的CP设置状态'''
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return
        
        # 检查管理员权限
        if not self._is_admin(event):
            yield event.plain_result("只有群主或管理员才能使用此指令~")
            return
        
        async with self.lock:
            group_data = self._get_group_data(group_id)
            mode = group_data.get("mode", "random")
            max_swaps = group_data.get("max_swaps", 1)
            mode_text = "绑定模式" if mode == "bind" else "非绑定模式"
            
            husbands_count = len(group_data.get("husbands", {}))
            wives_count = len(group_data.get("wives", {}))
        
        status = f"""=== CP 插件状态 ===
当前模式：{mode_text}
每天更换次数：{max_swaps} 次
已抽取老公：{husbands_count} 人
已抽取老婆：{wives_count} 人
日期：{group_data.get('date', '未知')}"""
        yield event.plain_result(status)

    # ========== 关系图 ==========

    @filter.command("关系图")
    async def relationship_graph(self, event: AstrMessageEvent):
        '''生成群友CP关系图'''
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("此插件仅支持 aiocqhttp (QQ) 平台")
            return
        
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此指令仅在群聊中可用")
            return
        
        async with self.lock:
            self._check_and_reset(group_id)
            group_data = self._get_group_data(group_id)
            husbands = group_data.get("husbands", {})
            wives = group_data.get("wives", {})
            
            if not husbands and not wives:
                yield event.plain_result("今天还没有人抽取 CP 呢，快使用「随机老公」或「随机老婆」吧~")
                return
            
            # 获取群成员信息
            members = await self._get_group_members(event)
        
        # 生成关系图
        try:
            image_path = await self._generate_relationship_graph(
                group_id, husbands, wives, members
            )
            if image_path and os.path.exists(image_path):
                result = event.make_result()
                result.chain = [
                    Comp.Plain("群友 CP 关系图："),
                    Comp.Image(file=image_path),
                ]
                yield result
            else:
                yield event.plain_result("生成关系图失败")
        except Exception as e:
            logger.error(f"生成关系图失败: {e}")
            yield event.plain_result(f"生成关系图失败: {e}")

    def _calculate_layout(self, n: int) -> List[tuple]:
        """计算多层同心圆布局，返回 [(radius, count, angle_offset), ...]"""
        avatar_size = 80
        padding = 15  # 减小边距，头像更靠近边界
        max_dim = 1024
        # 头像中心到画布中心的最大距离
        max_radius = max_dim / 2 - padding - avatar_size / 2
        
        min_gap = 8  # 弧长方向最小间距
        arc_per_avatar = avatar_size + min_gap  # 每个头像占用的弧长
        
        # 尝试单层布局
        min_radius_single = n * arc_per_avatar / (2 * 3.14159)
        if min_radius_single <= max_radius:
            radius = max(avatar_size * 0.6, min_radius_single)
            return [(radius, n, 0.0)]
        
        # 需要多层同心圆
        ring_spacing = 75  # 层间距（径向）
        min_layer_radius = avatar_size * 0.55  # 最内层最小半径
        
        # 从外向内构建各层半径
        radii = []
        r = max_radius
        while r >= min_layer_radius:
            radii.append(r)
            r -= ring_spacing
        
        if not radii:
            radii = [max_radius]
        
        # 计算每层最大容量
        capacities = [max(1, int(2 * 3.14159 * ri / arc_per_avatar)) for ri in radii]
        total_cap = sum(capacities)
        
        # 如果总容量不够，尝试减小参数
        if total_cap < n:
            # 先尝试减小弧长间距
            min_gap = 5
            arc_per_avatar = avatar_size + min_gap  # 85
            capacities = [max(1, int(2 * 3.14159 * ri / arc_per_avatar)) for ri in radii]
            total_cap = sum(capacities)
        
        # 如果还不够，尝试更小的层间距来增加层数
        if total_cap < n:
            for rs in [65, 55, 45]:
                radii = []
                r = max_radius
                while r >= min_layer_radius:
                    radii.append(r)
                    r -= rs
                if not radii:
                    radii = [max_radius]
                capacities = [max(1, int(2 * 3.14159 * ri / arc_per_avatar)) for ri in radii]
                total_cap = sum(capacities)
                if total_cap >= n:
                    ring_spacing = rs
                    break
        
        # 确定实际需要多少层
        num_layers = 0
        cumsum = 0
        for cap in capacities:
            cumsum += cap
            num_layers += 1
            if cumsum >= n:
                break
        
        radii = radii[:num_layers]
        capacities = capacities[:num_layers]
        
        # 按周长比例分配人数到各层
        circumferences = [2 * 3.14159 * ri for ri in radii]
        total_circumference = sum(circumferences)
        
        counts = []
        remaining = n
        for i in range(num_layers):
            if i == num_layers - 1:
                # 最后一层放剩余所有人
                c = min(remaining, capacities[i])
            else:
                # 按周长比例分配
                c = max(1, int(n * circumferences[i] / total_circumference))
                c = min(c, capacities[i])
                c = min(c, remaining)
            counts.append(c)
            remaining -= c
        
        # 处理剩余人数（从外层开始补）
        while remaining > 0:
            placed = False
            for i in range(num_layers):
                if counts[i] < capacities[i] and remaining > 0:
                    counts[i] += 1
                    remaining -= 1
                    placed = True
            if not placed:
                # 所有层都满了，强制放到最外层
                counts[0] += remaining
                remaining = 0
        
        # 构建结果，每层角度偏移交错
        result = []
        for i in range(num_layers):
            if counts[i] > 0:
                # 角度偏移：相邻层错开半个头像位置，避免径向对齐
                offset = (i * 3.14159 / max(num_layers, 2)) % (2 * 3.14159)
                result.append((radii[i], counts[i], offset))
        
        logger.info(f"关系图布局: 总人数{n}, 层数{len(result)}, 各层{[(int(r), c) for r, c, _ in result]}")
        return result

    async def _generate_relationship_graph(
        self, group_id: str, husbands: Dict, wives: Dict, members: List[Dict]
    ) -> Optional[str]:
        """生成关系图图片（支持多层同心圆布局）"""
        # 收集所有参与关系的用户
        involved_users = set()
        relations = []  # [(from_id, to_id, type)] type: 'husband' or 'wife'
        
        for user_id, target_id in husbands.items():
            involved_users.add(user_id)
            involved_users.add(target_id)
            relations.append((user_id, target_id, "husband"))
        
        for user_id, target_id in wives.items():
            involved_users.add(user_id)
            involved_users.add(target_id)
            relations.append((user_id, target_id, "wife"))
        
        if not involved_users:
            return None
        
        # 构建用户信息映射
        user_info = {}
        for user_id in involved_users:
            info = self._get_member_info(members, user_id)
            if info:
                user_info[user_id] = info
            else:
                user_info[user_id] = {
                    "user_id": user_id,
                    "display_name": f"用户{user_id}",
                }
        
        # 固定画布 1024×1024
        avatar_size = 80
        canvas_width = 1024
        canvas_height = 1024
        center_x = canvas_width // 2
        center_y = canvas_height // 2 - 30  # 稍微上移，给底部图例留空间
        
        # 创建画布
        img = Image.new("RGB", (canvas_width, canvas_height), (245, 248, 250))
        draw = ImageDraw.Draw(img)
        
        # 尝试加载字体
        font = None
        has_chinese_font = False
        try:
            font_paths = [
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
                "/usr/share/fonts/truetype/arphic/uming.ttc",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "C:/Windows/Fonts/msyh.ttc",
                "C:/Windows/Fonts/simhei.ttf",
                "C:/Windows/Fonts/simsun.ttc",
                "C:/Windows/Fonts/msyhbd.ttc",
            ]
            for fp in font_paths:
                if os.path.exists(fp):
                    font = ImageFont.truetype(fp, 16)
                    has_chinese_font = True
                    logger.info(f"加载字体: {fp}")
                    break
        except Exception as e:
            logger.warning(f"加载字体失败: {e}")
        
        if font is None:
            font = ImageFont.load_default()
            logger.warning("未找到中文字体，图片中的中文将显示为方框。建议在服务器安装中文字体（如 apt install fonts-wqy-zenhei）")
        
        # 计算多层同心圆布局
        n = len(involved_users)
        layout = self._calculate_layout(n)  # [(radius, count, angle_offset), ...]
        
        # 按层分配用户（从内层到外层）
        user_list = list(involved_users)
        positions = {}  # {user_id: (x, y)}
        user_idx = 0
        
        # 内层先分配，外层后分配（绘制时外层覆盖内层）
        for radius, count, offset in reversed(layout):
            for j in range(count):
                if user_idx >= len(user_list):
                    break
                user_id = user_list[user_idx]
                angle = offset + 2 * 3.1415926 * j / count - 3.1415926 / 2
                x = center_x + radius * __import__('math').cos(angle)
                y = center_y + radius * __import__('math').sin(angle)
                positions[user_id] = (x, y)
                user_idx += 1
        
        # 下载并绘制头像（从内层到外层，外层头像会覆盖内层）
        async with aiohttp.ClientSession() as session:
            for user_id in user_list:
                if user_id not in positions:
                    continue
                x, y = positions[user_id]
                
                # 尝试下载头像
                avatar_url = self._get_qq_avatar_url(user_id)
                try:
                    async with session.get(avatar_url, timeout=5) as resp:
                        if resp.status == 200:
                            avatar_data = await resp.read()
                            avatar = Image.open(BytesIO(avatar_data))
                        else:
                            avatar = None
                except Exception:
                    avatar = None
                
                # 如果下载失败，使用默认头像
                if avatar is None:
                    avatar = Image.new("RGB", (avatar_size, avatar_size), (200, 200, 200))
                    draw_avatar = ImageDraw.Draw(avatar)
                    draw_avatar.ellipse([0, 0, avatar_size-1, avatar_size-1], fill=(150, 150, 150))
                else:
                    avatar = avatar.resize((avatar_size, avatar_size), Image.LANCZOS)
                    # 裁剪为圆形
                    mask = Image.new("L", (avatar_size, avatar_size), 0)
                    draw_mask = ImageDraw.Draw(mask)
                    draw_mask.ellipse([0, 0, avatar_size-1, avatar_size-1], fill=255)
                    avatar = Image.composite(avatar, Image.new("RGB", (avatar_size, avatar_size), (245, 248, 250)), mask)
                
                # 绘制头像
                x1 = int(x - avatar_size / 2)
                y1 = int(y - avatar_size / 2)
                img.paste(avatar, (x1, y1))
                
                # 绘制昵称（只有找到中文字体时才显示）
                if has_chinese_font:
                    name = user_info.get(user_id, {}).get("display_name", f"用户{user_id}")
                    bbox = draw.textbbox((0, 0), name, font=font)
                    text_width = bbox[2] - bbox[0] if bbox else 0
                    text_x = int(x - text_width / 2)
                    text_y = int(y + avatar_size / 2 + 5)
                    
                    if text_width > 0:
                        draw.rectangle(
                            [text_x - 2, text_y - 1, text_x + text_width + 2, text_y + 18],
                            fill=(255, 255, 255, 180)
                        )
                    draw.text((text_x, text_y), name, fill=(30, 30, 30), font=font)
        
        # 绘制箭头（关系线在所有头像之上）
        for from_id, to_id, rel_type in relations:
            if from_id not in positions or to_id not in positions:
                continue
            
            x1, y1 = positions[from_id]
            x2, y2 = positions[to_id]
            
            # 计算箭头起点和终点（在头像边缘）
            dx = x2 - x1
            dy = y2 - y1
            dist = max(1, (dx**2 + dy**2) ** 0.5)
            
            # 起点和终点偏移头像半径
            start_x = x1 + (dx / dist) * (avatar_size / 2 + 5)
            start_y = y1 + (dy / dist) * (avatar_size / 2 + 5)
            end_x = x2 - (dx / dist) * (avatar_size / 2 + 5)
            end_y = y2 - (dy / dist) * (avatar_size / 2 + 5)
            
            # 颜色：老婆关系红色，老公关系蓝色
            color = (220, 50, 50) if rel_type == "wife" else (50, 100, 220)
            
            # 绘制线条
            draw.line([(start_x, start_y), (end_x, end_y)], fill=color, width=3)
            
            # 绘制箭头
            arrow_size = 12
            angle = __import__('math').atan2(dy, dx)
            arrow_angle1 = angle + 2.5
            arrow_angle2 = angle - 2.5
            
            ax1 = end_x - arrow_size * __import__('math').cos(arrow_angle1)
            ay1 = end_y - arrow_size * __import__('math').sin(arrow_angle1)
            ax2 = end_x - arrow_size * __import__('math').cos(arrow_angle2)
            ay2 = end_y - arrow_size * __import__('math').sin(arrow_angle2)
            
            draw.polygon([(end_x, end_y), (ax1, ay1), (ax2, ay2)], fill=color)
        
        # 绘制图例
        legend_padding = 20
        legend_y = canvas_height - 40
        # 红色箭头 = 老婆关系
        draw.line([(legend_padding, legend_y), (legend_padding + 30, legend_y)], fill=(220, 50, 50), width=3)
        draw.polygon([(legend_padding + 30, legend_y), (legend_padding + 22, legend_y - 6), (legend_padding + 22, legend_y + 6)], fill=(220, 50, 50))
        draw.text((legend_padding + 40, legend_y - 8), "老婆关系", fill=(220, 50, 50), font=font)
        
        # 蓝色箭头 = 老公关系
        draw.line([(legend_padding + 130, legend_y), (legend_padding + 160, legend_y)], fill=(50, 100, 220), width=3)
        draw.polygon([(legend_padding + 160, legend_y), (legend_padding + 152, legend_y - 6), (legend_padding + 152, legend_y + 6)], fill=(50, 100, 220))
        draw.text((legend_padding + 170, legend_y - 8), "老公关系", fill=(50, 100, 220), font=font)
        
        # 保存图片
        output_path = os.path.join(os.path.abspath(self.data_dir), f"graph_{group_id}.png")
        img.save(output_path, "PNG")
        return output_path

    async def terminate(self):
        """插件卸载时调用"""
        logger.info("群友 CP 随机抽取插件已卸载")
