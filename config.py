"""投喂插件 — 配置模型。"""

from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase

# 限制持久化覆盖值，避免超大整数在 SQLite 积分运算中溢出。
MAX_SIGN_BASE_POINTS = 1_000_000

# 清除型道具：shop_items.style_prompt 等于此标记时，吃下后清空该群全部生效的投喂风格
STYLE_CLEAR_ALL_MARKER = "__clear_all_styles__"
# 清除型道具在商店/回复中的展示文案
STYLE_CLEAR_ALL_LABEL = "清除全部投喂风格"

# 内置清除风格道具：每个启用群自动种一条 🥛牛奶（已存在任意同名行则跳过，
# 管理员下架是软下架、行仍在，因此不会被重新种回）
BUILTIN_CLEAR_ITEM_NAME = "牛奶"
BUILTIN_CLEAR_ITEM_EMOJI = "🥛"
BUILTIN_CLEAR_ITEM_DESC = "喝一口温牛奶，清掉本群全部投喂风格"
BUILTIN_CLEAR_ITEM_PRICE = 5
BUILTIN_CLEAR_ITEM_SATIETY = 5.0


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用投喂插件")
    config_version: str = Field(
        default="1.6.0",
        description="配置版本（由插件自动维护，与插件版本一致）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class AdminConfig(PluginConfigBase):
    """管理员配置。"""

    __ui_label__ = "管理员"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    admin_users: list[str] = Field(default_factory=list, description="Bot管理员QQ号列表")


class SignConfig(PluginConfigBase):
    """签到配置。"""

    __ui_label__ = "签到"
    __ui_icon__ = "calendar-check"
    __ui_order__ = 2

    base_points: int = Field(default=10, description="签到基础积分")
    consecutive_bonus: int = Field(default=5, description="连续签到额外奖励（每天递增）")
    max_consecutive_bonus: int = Field(default=50, description="连续签到奖励上限")


class BotAttrConfig(PluginConfigBase):
    """Bot属性配置。"""

    __ui_label__ = "Bot属性（bot_attr）"
    __ui_icon__ = "heart"
    __ui_order__ = 3

    initial_satiety: float = Field(
        default=80.0,
        description="初始饱食度（0-100）",
        json_schema_extra={"label": "初始饱食度（initial_satiety）"},
    )
    satiety_decay_rate: float = Field(
        default=0.5,
        description="饱食度每小时衰减量",
        json_schema_extra={"label": "饱食度衰减量（satiety_decay_rate）"},
    )
    seek_feed_threshold: float = Field(
        default=30.0,
        description="饱食度低于此值触发求投喂",
        json_schema_extra={"label": "求投喂阈值（seek_feed_threshold）"},
    )
    seek_feed_cooldown: float = Field(
        default=7200.0,
        description="求投喂消息冷却时间（秒，默认2小时）",
        json_schema_extra={"label": "求投喂冷却时间（seek_feed_cooldown）"},
    )
    quiet_hours: str = Field(
        default="23:00-08:00",
        description="求投喂安静时段，格式 'HH:MM-HH:MM'（24小时制），该时段内不发送求投喂消息；留空则不限制",
        json_schema_extra={"label": "安静时段（quiet_hours）"},
    )
    seek_feed_messages: list[str] = Field(
        default_factory=lambda: [
            "肚子有点饿了……谁来投喂我一下？",
            "肚子咕咕叫了，懒得自己找吃的……有人投喂吗？",
            "饿了饿了！今天想吃什么好呢，投喂我一下嘛。",
            "饿得没力气说话了……谁投喂我一口？",
            "好想吃点好吃的，饿得脑子都转不动了。",
            "有人投喂吗？就一口，我不贪心的。",
            "想被投喂了……投喂我的人今天运气会变好哦。",
            "肚子空了，懒得动……投喂我的话，会特别感谢你哦。",
            "闻到好吃的味道了……不对，是我饿了，谁来投喂？",
            "饿了就直说：想被投喂了。所以，谁来投喂我一下？",
            "趴在群里等投喂……投喂我一下好不好？",
            "今天也没人投喂吗……那我再饿一会儿。",
            "想吃点什么……什么都行，有人投喂吗？",
            "饿得连贫嘴的力气都没了……直接问：谁投喂我？",
            "一直盯着群看不是因为馋……只是刚好饿了。",
            "有气无力地冒了个泡……谁来投喂我一口？",
            "有没有人发现我今天还没吃东西……快来投喂我。",
            "饿了……但懒得说太多，就一句：投喂我。",
            "好像闻到饭香了……不对，我还没吃上呢，谁来投喂？",
            "冒个泡……顺便问问，有没有人投喂我呀？",
        ],
        description="求投喂消息列表，随机选择一条发送（LLM开启时优先用LLM生成）",
        json_schema_extra={"label": "求投喂消息（seek_feed_messages）"},
    )


class GroupAdminEntry(PluginConfigBase):
    """单个群的授权配置。"""

    __ui_label__ = "群配置"
    __ui_icon__ = "users"
    __ui_order__ = 0

    group_id: str = Field(default="", description="群号")
    admin_users: str = Field(
        default="",
        description="管理员QQ号（多个用逗号、空格或 | 分隔）",
    )

    def gid(self) -> str:
        """返回清理后的群号。"""
        return str(self.group_id or "").strip()

    def admin_list(self) -> list[str]:
        """返回解析后的管理员QQ列表。"""
        return [s.strip() for s in str(self.admin_users or "").replace(",", " ").replace("|", " ").split() if s.strip()]


class FilterConfig(PluginConfigBase):
    """触发控制配置。"""

    __ui_label__ = "触发控制"
    __ui_icon__ = "filter"
    __ui_order__ = 4

    group_admins_whitelist: bool = Field(
        default=True,
        description=(
            "按群隔离总开关（默认开启）。开启时本列表就是唯一白名单：只有列在这里的群"
            "会启用投喂功能（求投喂、签到、商店等），不在列表里的群一律关闭；"
            "把某个群从列表里删掉，下次配置热重载时该群就会被关闭。"
            "关闭后回到旧行为：本列表只做首次初始化，之后以数据库和 /投喂管理 授权的群为准"
        ),
    )
    group_admins: list[GroupAdminEntry] = Field(
        default_factory=list,
        description="白名单群配置，每项包含群号和管理员QQ。按群隔离开启时，只有这里的群才会启用投喂功能",
    )


class LLMConfig(PluginConfigBase):
    """LLM回复配置。"""

    __ui_label__ = "LLM回复"
    __ui_icon__ = "brain"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="是否使用LLM生成投喂回复")
    model: str = Field(
        default="replyer",
        description=(
            "MaiBot 模型任务名，对应 model_config.toml 中 [model_task_config.X] 的 X"
            "（如 replyer / planner / utils）。插件会把它作为 task_name 传给宿主，留空回退 replyer。"
            "注意填的是【任务名】不是具体模型名——填模型名会报「未找到名为 X 的模型」"
        ),
    )
    temperature: float = Field(default=0.8, description="LLM温度（越高越随机）")
    max_tokens: int = Field(
        default=2000,
        description=(
            "LLM最大输出token数（含 reasoning 思考token）。推理模型（如 deepseek-v4-pro）会先花掉"
            "大量 token 思考，此值太小会导致思考没结束就被截断、拿不到正式回复而退回兜底文案；"
            "建议 1500-4000"
        ),
    )
    timeout_seconds: float = Field(
        default=120.0,
        description=(
            "LLM生成回复的最长等待时间（秒）。宿主给插件调用的默认RPC超时只有30秒，"
            "模型较慢时LLM还没返回就被判定失败、退回兜底回复；此项用于放宽等待，建议 60-150"
        ),
    )
    fallback_reply: list[str] = Field(
        default_factory=lambda: [
            "谢谢你投喂我！好开心~",
            "收到啦，谢谢投喂！我很喜欢~",
            "哇，好好吃！谢谢投喂，一下子就有精神了！",
            "谢谢投喂~我会好好珍惜这份心意的！",
            "才、才不是特别期待呢……不过谢谢投喂啦。",
            "投喂收到！开心得都要转圈圈了，谢谢~",
            "嗯……好吃。谢谢投喂，我很满足。",
            "才不是因为你投喂才开心呢……总之谢谢啦。",
            "谢谢投喂~吃饱了，又能继续摸鱼了。",
            "收到投喂了！我会记住这份心意的，谢谢~",
        ],
        description="LLM不可用时的兜底回复，随机抽选一条",
    )

    @property
    def effective_model(self) -> str:
        """获取实际使用的模型任务名，空字符串时默认使用 replyer。"""
        return self.model.strip() or "replyer"


class StyleConfig(PluginConfigBase):
    """投喂风格配置。"""

    __ui_label__ = "投喂风格"
    __ui_icon__ = "sparkles"
    __ui_order__ = 6

    max_active_effects: int = Field(
        default=3,
        description="同一群同时生效的投喂风格数量上限（达到上限后，喂不同道具不再叠加，喂同名道具则续期）",
    )
    expire_notice: bool = Field(
        default=True,
        description="风格失效时是否在群内发送失效提示（如「XX」效果已失效）",
    )


class FeedBotConfig(PluginConfigBase):
    """投喂插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    sign: SignConfig = Field(default_factory=SignConfig)
    bot_attr: BotAttrConfig = Field(default_factory=BotAttrConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    style: StyleConfig = Field(default_factory=StyleConfig)
