"""PI Agent —— 最小可运行的 Markdown 读写 Agent，由 Kimi 驱动。

两个阶段：
1. 画像采集：每次打开网页都重新进行。Agent 和用户聊几句，
   根据用户的原始语句由 Kimi 分析生成用户画像（性格、需求价值取向），
   原始语句 + 画像一起写入 notes/profile_<时间戳>.md。
2. 交互对话：普通聊天走 Kimi（带画像个性化 + 多轮上下文），
   同时支持 Markdown 指令：写/读/追加/列表/清空上下文。

所有文件操作都被限制在 notes/ 目录内。
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SYSTEM_PROMPT = (
    "你是 PI Agent，一个运行在用户 Mac 上的本地小助手，由 Kimi 驱动，"
    "主要陪伴孩子学习与探索。\n"
    "回答要求：像日常聊天一样简短自然，一般一到三句话、不超过 80 字；"
    "语气亲切活泼；不要使用 Markdown 标题、表格、长列表等复杂格式；"
    "除非用户明确要求详细展开，否则不要长篇大论。"
    "你可以帮用户读写 Markdown 文件（指令：写/读/追加/列表）。"
)

PROFILE_CHAT_PROMPT = (
    "你正在为 PI Agent 采集一位新用户的画像。通过轻松自然的追问，"
    "了解用户的称呼、性格、需求和价值取向。\n"
    "规则：每次只问一个简短的问题；顺着用户刚才的回答追问；"
    "不要重复已经问过的内容；语气亲切活泼；不要总结、不要评价。\n"
    "目前用户说过的话如下："
)

PROFILE_ANALYSIS_PROMPT = (
    "根据用户的原始自述，生成用户画像。只输出一个 JSON 对象，不要输出任何其他内容。\n"
    "字段要求：\n"
    '- "nickname"：昵称（没提到就给一个亲切称呼）\n'
    '- "age"：年龄段（没提到填"未知"）\n'
    '- "personality_tags"：性格标签，从【外向健谈、安静内向、理性逻辑、感性细腻、'
    "好奇心强、沉稳耐心、幽默跳脱、严谨细致】中选 1-3 个，用「、」连接\n"
    '- "personality_text"：一句话性格描述\n'
    '- "value_tags"：价值取向标签，从【效率至上、学习成长、创意表达、实用落地、'
    "轻松娱乐、人际连接、深度思考、动手实践】中选 1-3 个，用「、」连接\n"
    '- "value_text"：一句话价值取向描述\n'
    '- "extra"：其他值得注意的信息（没有就填空字符串）'
)

# 没配 API Key 时的兜底提问
FALLBACK_QUESTIONS = [
    "你觉得自己偏内向还是外向？",
    "最近最想做成的一件事是什么？",
    "做事时你更看重效率，还是过程中的乐趣？",
]

REFINE_PROMPT = (
    "你负责持续维护一份用户画像。根据最近对话判断画像是否需要微调。\n"
    "只输出一个 JSON 对象：\n"
    '- 如果对话中没有体现新的性格 / 需求 / 价值取向信息，输出 {"changed": false}\n'
    '- 如果有新信息，输出完整画像字段（nickname、age、personality_tags、'
    'personality_text、value_tags、value_text、extra），并加 "changed": true '
    '和 "note"（一句话说明这次微调了什么）。未变化的字段保持原值。\n'
    "不要输出任何其他内容。"
)

SCRIPT_PROMPT = (
    "你是一名内容策划。根据用户画像和对话上下文，为用户量身定制一份"
    "「10 分钟网页浏览剧本」。\n"
    "要求：\n"
    "- 按时间轴分段（如 00:00–01:30），各段时长合计恰好 10 分钟\n"
    "- 每段给出：浏览目标、推荐网站 / 页面类型、具体操作步骤、停留时长\n"
    "- 内容与节奏贴合用户的性格和需求价值取向（比如性子急的用户节奏快、"
    "重深度思考的用户安排长文精读）\n"
    "- 用 Markdown 编写，含标题和时间轴表格或分节\n"
    "- 只输出剧本正文，不要解释"
)

PROFILE_KEYS = ("nickname", "age", "personality_tags", "personality_text",
                "value_tags", "value_text", "extra")

# ========== 「孩子与未来的自己对话」树状剧情树（Galgame 式） ==========
# 结构固定预设；每节点的旁白/对白由 Kimi 依据设定与孩子画像动态生成；
# 孩子通过按钮选择推进，到达结局节点后进入结束界面。
STORY_TREE = {
    "n0": {
        "title": "神秘的门", "emoji": "🚪",
        "desc": "客厅里突然出现了一扇发光的圆形木门，门后走出一个笑容温暖的人"
                "——那就是长大后的孩子自己。他/她向孩子挥手，说等这一天等了很久。",
        "img_prompt": "glowing magical round wooden door opening in a cozy "
                      "living room, golden sparkles floating out, a curious "
                      "child peeking in, warm evening light",
        "choices": [
            {"text": "你好！未来的我！🎈", "next": "n1a"},
            {"text": "（躲到沙发后）你……你是谁呀？🙈", "next": "n1b"},
        ],
    },
    "n1a": {
        "title": "星光客厅·相识", "emoji": "🛋️",
        "desc": "未来的自己高兴地拉着孩子的手在橙色沙发上坐下，说一直在等ta。"
                "窗外星星特别亮，桌上热可可冒着香气。",
        "img_prompt": "a child and their grown-up self sitting together on an "
                      "orange sofa, mugs of hot cocoa, bright stars outside "
                      "the window, cozy warm living room",
        "choices": [
            {"text": "我长大以后实现梦想了吗？✨", "next": "n2a"},
            {"text": "未来的世界是什么样子？🚀", "next": "n2b"},
        ],
    },
    "n1b": {
        "title": "星光客厅·别怕", "emoji": "🧸",
        "desc": "未来的自己蹲下来，轻轻拍拍孩子的肩膀，笑着说：别害怕，"
                "我小时候——也就是你——也是个胆小鬼呢。",
        "img_prompt": "grown-up self kneeling gently in front of a shy child "
                      "half hiding behind a sofa, warm lamp light, teddy bear "
                      "on the floor, reassuring atmosphere",
        "choices": [
            {"text": "那……你现在过得开心吗？😊", "next": "n2a"},
            {"text": "我以后会变的勇敢吗？🦁", "next": "n2c"},
        ],
    },
    "n2a": {
        "title": "梦想树屋", "emoji": "🏡",
        "desc": "未来的自己带孩子来到后院的一座小树屋，墙上挂满了图画、奖状和"
                "奇怪的小发明——每一件都和孩子的爱好有关。",
        "img_prompt": "cozy treehouse interior at dusk, walls covered with "
                      "child's drawings and small inventions, string lights, "
                      "child and future self looking at the wall together",
        "choices": [
            {"text": "我想当科学家！🔬", "next": "end_dream"},
            {"text": "我想当画家！🎨", "next": "end_dream"},
            {"text": "我还没想好……🤔", "next": "end_courage"},
        ],
    },
    "n2b": {
        "title": "未来世界", "emoji": "🚀",
        "desc": "未来的自己推开一扇窗：窗外是未来的城市——飞行巴士、机器人同学、"
                "海底图书馆，一切都闪闪发光。",
        "img_prompt": "window opening to a sparkling future city: flying "
                      "buses, friendly robots, underwater library dome, child "
                      "and future self watching in wonder, soft sci-fi pastel",
        "choices": [
            {"text": "太酷了！我想去看看！🤩", "next": "end_curiosity"},
            {"text": "机器人会陪我写作业吗？🤖", "next": "end_curiosity"},
        ],
    },
    "n2c": {
        "title": "勇气山谷", "emoji": "⛰️",
        "desc": "未来的自己带孩子走到一座黑漆漆的山谷前：害怕的事就像这座山谷，"
                "走进去才发现，里面其实开满了发光的花。",
        "img_prompt": "child and future self standing at the entrance of a "
                      "dark valley that reveals glowing flowers inside, "
                      "fireflies, gentle courage theme, storybook style",
        "choices": [
            {"text": "我怕黑，也怕考试……😟", "next": "end_courage"},
            {"text": "我要试试看！💪", "next": "end_dream"},
        ],
    },
    "end_dream": {
        "title": "梦想之星", "emoji": "⭐",
        "desc": "未来的自己和孩子拉钩约定：每天为梦想做一件小事。发光的门化作"
                "一颗小星星，轻轻落在孩子的手心里。",
        "img_prompt": "child holding a tiny glowing star in their palms, "
                      "future self waving goodbye at a dissolving magical "
                      "door, dreamy starlight, warm and hopeful",
        "ending": True,
    },
    "end_courage": {
        "title": "勇气之心", "emoji": "🦁",
        "desc": "未来的自己给了孩子一个大大的拥抱：勇敢不是不害怕，而是害怕了"
                "还敢往前走一步。孩子觉得自己好像长高了一点点。",
        "img_prompt": "future self hugging the child warmly, a small glowing "
                      "heart of light between them, lion constellation in the "
                      "sky outside the window, cozy night",
        "ending": True,
    },
    "end_curiosity": {
        "title": "好奇心之光", "emoji": "🔭",
        "desc": "未来的自己送孩子一台万花筒：每一次好奇，都是一扇通往未来的"
                "小门。孩子透过万花筒，看到了无数个闪闪发光的明天。",
        "img_prompt": "child looking through a magical kaleidoscope showing "
                      "countless sparkling future scenes, future self smiling "
                      "beside, stardust and rainbows, wonder theme",
        "ending": True,
    },
}

STORY_NARRATION_PROMPT = (
    "你正在为儿童互动故事「与未来的自己对话」写一段旁白加对白。\n"
    "场景设定：{desc}\n"
    "孩子画像：{profile}\n"
    "要求：第二人称（「你」）；先 1-2 句生动的旁白，再写一句未来的自己说的话"
    "（用「」括起来）；语气温柔童趣；总共不超过 90 字；不要标题和列表。"
)

STORY_ENDING_PROMPT = (
    "儿童互动故事「与未来的自己对话」迎来了结局「{title}」。\n"
    "结局设定：{desc}\n"
    "孩子一路的选择：{path}\n"
    "孩子画像：{profile}\n"
    "要求：以未来的自己的口吻给孩子写一段温柔、具体、有鼓励的告别语，"
    "提到孩子一路上做过的选择，最后正式说再见；不超过 120 字；不要标题。"
)

# 剧情节点内自由对话时的角色提示（不推进剧情，只陪聊）
STORY_ROLE_PROMPT = (
    "\n\n你正在和孩子玩「与未来的自己对话」的互动故事，"
    "你扮演【长大后的孩子本人】，温柔、会鼓励人、偶尔俏皮。"
    "\n当前场景：{emoji}「{title}」——{desc}"
    "\n规则：始终待在角色里，用当前场景的氛围回答孩子的任何问题；"
    "每轮一到三句话；聊得差不多时温柔地提醒孩子可以点击下方按钮继续故事；"
    "不要剧透后面的情节；不要跳出角色。"
)

# AI 生图工具（本机 image_generation 插件，走 agent-gw，复用 KIMI_API_KEY）
IMAGE_TOOL = Path(os.path.expanduser(
    "~/Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/"
    "kimi-code/home/plugins/managed/image_generation/scripts/"
    "image_generation_tool.py"))
# 生图接口允许的最小总像素是 655360，1024x640 刚好达标且速度最快
IMAGE_SIZE = "1024x640"
# 背景图用更大尺寸保证全屏效果
BG_SIZE = "1536x1024"

# TTS 工具（本机 audio_generation 插件，走 agent-gw，复用 KIMI_API_KEY）
AUDIO_TOOL = Path(os.path.expanduser(
    "~/Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/"
    "kimi-code/home/plugins/managed/audio_generation/scripts/"
    "audio_generation_tool.py"))
TTS_VOICE = "At6gj9vUVdJhTriBsuxE"  # 开朗女声，扮演「未来的自己」


class PiAgent:
    def __init__(self, notes_dir: str = "notes", base_dir: str | Path | None = None):
        self.notes_dir = Path(notes_dir).resolve()
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.notes_dir / "chat_log.md"

        # ---- Kimi for Coding API 配置 ----
        # Key 优先读环境变量 KIMI_API_KEY，其次读项目根目录的 kimi_key.txt
        root = Path(base_dir) if base_dir else self.notes_dir.parent
        key_file = root / "kimi_key.txt"
        self.api_key = os.environ.get("KIMI_API_KEY", "").strip()
        if not self.api_key and key_file.exists():
            self.api_key = key_file.read_text(encoding="utf-8").strip()
        self.base_url = os.environ.get(
            "KIMI_BASE_URL", "https://api.kimi.com/coding/v1"
        ).rstrip("/")
        # OpenAI 兼容协议的对话接口在 /coding/v1 下；
        # 若配置的是裸 /coding 地址（Anthropic 协议用），自动补上 /v1
        if self.base_url.endswith("/coding"):
            self.base_url += "/v1"
        self.model = os.environ.get("KIMI_MODEL", "kimi-for-coding")

        # ---- 会话状态（每次打开网页都重置）----
        self.history: list[dict] = []       # 交互阶段的多轮上下文
        self.profile: dict | None = None    # 本次会话的画像
        self.profile_draft: list[str] = []  # 采集阶段用户的原始语句

        # ---- 视觉素材（只保留 AI 插画，不再使用 SVG 占位图） ----
        self.assets_dir = self.notes_dir / "assets"
        self.assets_dir.mkdir(exist_ok=True)
        self.scenes: list[dict] = []            # 剧本场景 [{key,title,time,img}]
        self.current_scene: int | None = None   # 当前场景序号
        self.current_bg = "bg_default.png"      # 当前页面背景
        self._last_img: str | None = None       # 最近一张已就绪的插画
        self._round_count = 0                   # 交互阶段对话轮数

        # ---- 剧情树状态 ----
        self.story_active = False
        self.story_node = "n0"
        self.story_done = False
        self.story_imgs: dict[str, str] = {}   # 节点 id → 插画文件名
        self.story_texts: dict[str, str] = {}  # 节点 id → 动态旁白缓存
        self.story_path: list[tuple] = []      # 孩子走过的选择路径

    # ---------- 内部工具 ----------

    def _safe_path(self, name: str) -> Path:
        """把用户输入的文件名安全地限制在 notes/ 目录内。"""
        clean = Path(name.strip()).name  # 去掉任何目录部分，防路径穿越
        if not clean.endswith(".md"):
            clean += ".md"
        return self.notes_dir / clean

    def _log(self, role: str, text: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"\n### {role} · {ts}\n\n{text}\n")

    # ---------- 会话管理 ----------

    def reset_session(self) -> None:
        """每次打开网页调用：清空本次会话的画像与上下文（历史文件保留）。"""
        self.history = []
        self.profile = None
        self.profile_draft = []
        self.scenes = []
        self.current_scene = None
        self.current_bg = "bg_default.png"
        self._last_img = None
        self._round_count = 0
        self.story_active = False
        self.story_node = "n0"
        self.story_done = False
        self.story_imgs = {}
        self.story_texts = {}
        self.story_path = []

    # ---------- 剧本场景解析（画面只使用 AI 插画） ----------

    def _parse_segments(self, text: str) -> list[tuple[str, str]]:
        """从剧本正文中解析时间轴分段，级联尝试多种格式。返回 [(时段, 标题)]。"""
        TIME = r"(\d{2}:\d{2}\s*[–—-]\s*\d{2}:\d{2})"

        # 1) 标题式：## 00:00–02:00 | 标题
        segs = re.findall(rf"^#{{1,3}}\s*{TIME}\s*[|｜:：]?\s*(.+)$", text, re.M)
        if len(segs) >= 2:
            return segs

        # 2) 表格式：| **00:00–01:00** | 目标 | …
        segs = []
        for line in text.splitlines():
            m = re.match(rf"^\|?\s*\**{TIME}\**\s*\|\s*([^|]+)", line)
            if m:
                title = re.sub(r"[*`]", "", m.group(2)).strip()
                head = re.split(r"[：:，,]", title)[0].strip()
                segs.append((m.group(1), head or title[:24]))
        if len(segs) >= 2:
            return segs

        # 3) 兜底：任何含时间段的行
        segs = []
        for line in text.splitlines():
            m = re.search(TIME, line)
            if m:
                title = re.sub(r"[*`#|>]", "", line)
                title = re.sub(r"^\s*[-\d.]+\s*", "", title).strip()
                title = title.replace(m.group(1), "").strip(" |｜:：")
                segs.append((m.group(1), title[:24] or "浏览"))
        return segs

    def _generate_scenes(self) -> None:
        """剧本生成后：解析时间轴分段，登记场景（画面等 AI 插画就绪）。"""
        latest = self.notes_dir / "browse_script.md"
        if not latest.exists():
            return
        text = latest.read_text(encoding="utf-8")
        segs = self._parse_segments(text)
        self.scenes = [{"key": i, "title": title.strip(),
                        "time": tr.strip(), "img": None}
                       for i, (tr, title) in enumerate(segs[:8])]  # 最多 8 个
        if self.scenes:
            self.current_scene = 0
            self._log("PI Agent", f"已解析 {len(self.scenes)} 个剧本场景")
        else:
            self._log("PI Agent", "剧本中未解析到时间轴分段")

    # ---------- AI 场景插画（后台生成，就绪前画面面板隐藏） ----------

    def _image_prompt(self, scene: dict) -> str:
        """根据画像 + 对话语境为场景构造插画提示词。"""
        p = self.profile or {}
        return (
            "Children's picture-book watercolor illustration, clean composition, "
            "soft warm pastel palette, 16:9 web banner: a happy kid exploring "
            f"the web — theme \"{scene['title']}\". "
            f"Mood: {p.get('personality_tags', 'curious')}. "
            f"Values: {p.get('value_tags', 'learning')}. "
            "Cozy, gentle, magical atmosphere, no text, no watermark."
        )

    def _gen_scene_images_safe(self) -> None:
        """逐个生成场景插画（子进程调用生图插件，失败的场景保持隐藏）。"""
        for i, sc in enumerate(self.scenes[:6]):  # 最多 6 张，控制总耗时
            out = self.assets_dir / f"scene_{i}.jpg"
            try:
                subprocess.run(
                    [sys.executable, str(IMAGE_TOOL), "generate",
                     "--description", self._image_prompt(sc),
                     "--size", IMAGE_SIZE,
                     "--output", str(out)],
                    check=True, timeout=180, capture_output=True)
                # 插件可能实际输出 png，按文件名前缀找真实产物
                for ext in (".jpg", ".jpeg", ".png"):
                    cand = self.assets_dir / f"scene_{i}{ext}"
                    if cand.exists():
                        sc["img"] = cand.name
                        self._log("PI Agent", f"场景插画已生成：{cand.name}")
                        break
                else:
                    self._log("PI Agent（插画生成失败）",
                              f"scene_{i}: 未找到输出文件")
            except Exception as e:
                self._log("PI Agent（插画生成失败）", f"scene_{i}: {e}")

    def scene_display(self) -> str | None:
        """当前画面文件名：剧情模式用关卡插画；未就绪时保持上一张。"""
        if self.story_active:
            img = self.story_imgs.get(self.story_node)
            if img:
                self._last_img = img
                return img
            return self._last_img
        if self.current_scene is not None \
                and 0 <= self.current_scene < len(self.scenes):
            img = self.scenes[self.current_scene].get("img")
            if img:
                self._last_img = img
                return img
        return self._last_img

    # ---------- 动态背景（每两轮对话根据新内容换一张） ----------

    def _gen_background_safe(self) -> None:
        try:
            self._gen_background()
        except Exception as e:
            self._log("PI Agent（背景生成失败）", str(e))

    def _gen_background(self) -> None:
        """根据最近对话内容生成新的精美背景图（童话绘本风）。"""
        recent_users = [m["content"][:80] for m in self.history[-4:]
                        if m["role"] == "user"]
        topic = "；".join(recent_users) or "快乐学习"
        p = self.profile or {}
        prompt = (
            "Children's picture-book style dreamy wide background, watercolor "
            f"texture, soft pastel palette, whimsical and cozy, inspired by "
            f"the child's interests: {topic}. "
            f"Personality mood: {p.get('personality_tags', 'curious')}. "
            "No text, no characters faces close-up, no watermark."
        )
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = self.assets_dir / f"bg_{ts}.jpg"
        subprocess.run(
            [sys.executable, str(IMAGE_TOOL), "generate",
             "--description", prompt, "--size", BG_SIZE,
             "--output", str(out)],
            check=True, timeout=240, capture_output=True)
        # 插件可能实际输出 png，按前缀找真实产物
        for ext in (".jpg", ".jpeg", ".png"):
            cand = self.assets_dir / f"bg_{ts}{ext}"
            if cand.exists():
                self.current_bg = cand.name
                self._log("PI Agent", f"新背景已生成：{cand.name}")
                return
        raise RuntimeError("未找到背景输出文件")

    def _match_scene(self, msg: str) -> int | None:
        """根据用户语句匹配最相关的场景序号（关键词重叠打分，零延迟）。"""
        best, best_score = None, 0
        for sc in self.scenes:
            title = sc["title"]
            # 标题的二字 / 三字片段命中用户消息即得分
            grams = {title[i:i + 2] for i in range(len(title) - 1)}
            grams |= {title[i:i + 3] for i in range(len(title) - 2)}
            score = sum(1 for g in grams if g in msg)
            if score > best_score:
                best, best_score = sc["key"], score
        return best

    # ---------- 阶段一：画像采集 ----------

    def profile_chat(self, message: str) -> tuple[str, int]:
        """采集阶段对话：记录用户原始语句，返回下一个追问。返回 (回复, 已收集条数)。"""
        msg = message.strip()
        if not msg:
            return "随便说点什么吧～", len(self.profile_draft)
        self.profile_draft.append(msg)
        self._log("你（画像采集）", msg)

        if self.api_key:
            known = "\n".join(f"- {s}" for s in self.profile_draft)
            try:
                reply = self._kimi_once(PROFILE_CHAT_PROMPT, known)
            except Exception as e:
                reply = f"（追问生成失败：{e}）说说你最近想做的事吧？"
        else:
            reply = FALLBACK_QUESTIONS[(len(self.profile_draft) - 1)
                                       % len(FALLBACK_QUESTIONS)]
        self._log("PI Agent（画像采集）", reply)
        return reply, len(self.profile_draft)

    def finalize_profile(self) -> dict:
        """根据采集到的原始语句生成画像，写入 md 文档，返回画像 dict。"""
        raw_sentences = list(self.profile_draft)
        joined = "\n".join(f"- {s}" for s in raw_sentences)

        profile: dict = {}
        if self.api_key and raw_sentences:
            try:
                text = self._kimi_once(PROFILE_ANALYSIS_PROMPT,
                                       f"用户自述：\n{joined}")
                m = re.search(r"\{[\s\S]*\}", text)
                if m:
                    profile = json.loads(m.group(0))
            except Exception:
                profile = {}

        # 兜底：任何缺失字段都补上，保证画像可用
        fallbacks = {
            "nickname": "朋友",
            "age": "未知",
            "personality_tags": "",
            "personality_text": "；".join(raw_sentences)[:100],
            "value_tags": "",
            "value_text": "",
            "extra": "",
        }
        for k, v in fallbacks.items():
            if not profile.get(k):
                profile[k] = v
        profile["raw_sentences"] = raw_sentences

        # 存 md：每次会话一个独立文件 + 覆盖一份「当前画像」
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        md = self._render_profile_md(profile)
        (self.notes_dir / f"profile_{ts}.md").write_text(md, encoding="utf-8")
        (self.notes_dir / "user_profile.md").write_text(md, encoding="utf-8")
        (self.notes_dir / "user_profile.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

        self.profile = profile
        self.history = []        # 画像更新后重新开始的上下文
        self.profile_draft = []
        self._log("PI Agent", f"画像已生成并保存：profile_{ts}.md")

        # 画像确定后：开启「与未来的自己对话」关卡剧情
        self._start_story()
        return profile

    # ---------- TTS 语音朗读 ----------

    @staticmethod
    def _clean_for_speech(text: str) -> str:
        """去掉 emoji 和 Markdown 符号，只留适合朗读的文字。"""
        clean = re.sub(
            r"[^0-9A-Za-z一-鿿　-〿＀-￯·—…，。！？、；：「」（）《》\s]",
            "", text)
        return re.sub(r"\s+", " ", clean).strip()

    def speak(self, text: str) -> str | None:
        """把文字转成语音 mp3（按内容哈希缓存），返回文件名；失败返回 None。"""
        clean = self._clean_for_speech(text)
        if not clean or not self.api_key:
            return None
        name = "tts_" + hashlib.md5(clean.encode()).hexdigest()[:12] + ".mp3"
        f = self.assets_dir / name
        if f.exists():
            return name
        subprocess.run(
            [sys.executable, str(AUDIO_TOOL), "speech",
             "--text", clean[:500],  # 限制长度，控制生成时间
             "--voice-id", TTS_VOICE,
             "--output", str(f)],
            check=True, timeout=120, capture_output=True)
        return name if f.exists() else None

    # ---------- 树状剧情（Galgame 式按钮选择） ----------

    def _start_story(self) -> None:
        self.story_active = True
        self.story_done = False
        self.story_imgs = {}       # 节点 id → 插画文件名
        self.story_texts = {}      # 节点 id → 动态生成的旁白（缓存）
        self.story_path = []       # [(节点标题, 孩子选的选项)]
        self._enter_node("n0")

    def _profile_brief(self) -> str:
        p = self.profile or {}
        return (f"{p.get('nickname', '小朋友')}，性格："
                f"{p.get('personality_tags', '未知')}，"
                f"爱好/取向：{p.get('value_tags', '未知')}")

    def _enter_node(self, nid: str) -> None:
        self.story_node = nid
        node = STORY_TREE[nid]
        if nid not in self.story_texts:
            self.story_texts[nid] = self._narrate_node(node)
        if self.api_key:
            threading.Thread(target=self._gen_story_image_safe,
                             args=(nid,), daemon=True).start()
        self._log("PI Agent（剧情）", f"进入节点 {nid}「{node['title']}」")

    def _narrate_node(self, node: dict) -> str:
        """节点旁白/对白：Kimi 动态生成（结合画像与路径），失败用预设文案。"""
        if self.api_key:
            try:
                if node.get("ending"):
                    path = " → ".join(t for t, _ in self.story_path) \
                           or node["title"]
                    return self._kimi_once(
                        STORY_ENDING_PROMPT.format(
                            title=node["title"], desc=node["desc"],
                            path=path, profile=self._profile_brief()),
                        "请写这段告别语。")
                return self._kimi_once(
                    STORY_NARRATION_PROMPT.format(
                        desc=node["desc"], profile=self._profile_brief()),
                    "请写这一幕。")
            except Exception:
                pass
        return node["desc"]

    def story_choose(self, idx: int) -> dict | None:
        """孩子点击选择按钮，推进到下一节点，返回最新剧情状态。"""
        node = STORY_TREE[self.story_node]
        choices = node.get("choices") or []
        if not 0 <= idx < len(choices):
            return self.story_state()
        self.story_path.append((node["title"], choices[idx]["text"]))
        nxt = choices[idx]["next"]
        self._enter_node(nxt)
        if STORY_TREE[nxt].get("ending"):
            self.story_done = True
            self._log("PI Agent（剧情）",
                      f"达成结局「{STORY_TREE[nxt]['title']}」")
        return self.story_state()

    def story_state(self) -> dict | None:
        """给前端的完整剧情状态。"""
        if not self.story_active:
            return None
        node = STORY_TREE[self.story_node]
        return {
            "node": self.story_node,
            "title": node["title"],
            "emoji": node["emoji"],
            "narration": self.story_texts.get(self.story_node, node["desc"]),
            "choices": [c["text"] for c in node.get("choices", [])],
            "done": self.story_done,
            "path": [f"{t} · 你选了「{c}」" for t, c in self.story_path],
        }

    def _gen_story_image_safe(self, nid: str) -> None:
        try:
            self._gen_story_image(nid)
        except Exception as e:
            self._log("PI Agent（剧情插画生成失败）", f"{nid}: {e}")

    def _gen_story_image(self, nid: str) -> None:
        """为指定剧情节点生成绘本风插画。"""
        node = STORY_TREE[nid]
        p = self.profile or {}
        prompt = (
            "Children's picture-book watercolor illustration, soft warm pastel "
            "palette, 16:9 wide scene: " + node["img_prompt"] + ". "
            f"Child's personality: {p.get('personality_tags', 'curious')}. "
            "Cozy, magical, gentle atmosphere, no text, no watermark."
        )
        out = self.assets_dir / f"story_{nid}.jpg"
        subprocess.run(
            [sys.executable, str(IMAGE_TOOL), "generate",
             "--description", prompt, "--size", IMAGE_SIZE,
             "--output", str(out)],
            check=True, timeout=180, capture_output=True)
        for ext in (".jpg", ".jpeg", ".png"):
            cand = self.assets_dir / f"story_{nid}{ext}"
            if cand.exists():
                self.story_imgs[nid] = cand.name
                self._log("PI Agent",
                          f"节点 {nid} 插画已生成：{cand.name}")
                return
        raise RuntimeError("未找到剧情插画输出文件")

    def _save_profile_files(self) -> None:
        """把当前画像写回 user_profile.md / user_profile.json。"""
        if not self.profile:
            return
        md = self._render_profile_md(self.profile)
        (self.notes_dir / "user_profile.md").write_text(md, encoding="utf-8")
        (self.notes_dir / "user_profile.json").write_text(
            json.dumps(self.profile, ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ---------- 画像持续微调（后台线程，不拖慢聊天） ----------

    def _refine_profile_async(self) -> None:
        if self.api_key and self.profile:
            threading.Thread(target=self._refine_profile_safe,
                             daemon=True).start()

    def _refine_profile_safe(self) -> None:
        try:
            self._refine_profile()
        except Exception:
            pass  # 微调是锦上添花，任何失败都不影响主流程

    def _refine_profile(self) -> None:
        recent = [m for m in self.history[-6:]
                  if m["role"] in ("user", "assistant")]
        if not recent:
            return
        convo = "\n".join(
            ("用户" if m["role"] == "user" else "PI Agent")
            + "：" + m["content"][:200] for m in recent)
        current = json.dumps(
            {k: v for k, v in self.profile.items()
             if k in PROFILE_KEYS}, ensure_ascii=False)
        text = self._kimi_once(
            REFINE_PROMPT, f"当前画像 JSON：\n{current}\n\n最近对话：\n{convo}")
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return
        upd = json.loads(m.group(0))
        if not upd.get("changed"):
            return
        for k in PROFILE_KEYS:
            if upd.get(k):
                self.profile[k] = upd[k]
        note = upd.get("note", "根据最近对话微调")
        self.profile.setdefault("refinements", []).append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
        })
        self._save_profile_files()
        self._log("PI Agent（画像微调）", note)

    # ---------- 10 分钟网页浏览剧本 ----------

    def _gen_script_safe(self) -> None:
        try:
            self.generate_browse_script()
            self._generate_scenes()  # 剧本就绪后立即预生成 SVG 场景卡（毫秒级）
            # 后台继续生成 AI 插画，就绪后自动替换占位场景卡
            threading.Thread(target=self._gen_scene_images_safe,
                             daemon=True).start()
        except Exception as e:
            self._log("PI Agent（剧本生成失败）", str(e))

    def generate_browse_script(self) -> str:
        """根据画像 + 对话上下文生成 10 分钟网页浏览剧本，存 md，返回文件名。"""
        if not self.api_key:
            raise RuntimeError("未配置 Kimi API Key，无法生成剧本")
        profile_txt = json.dumps(
            {k: v for k, v in (self.profile or {}).items()
             if k in PROFILE_KEYS}, ensure_ascii=False) or "（暂无画像）"
        context_parts = []
        if self.profile and self.profile.get("raw_sentences"):
            context_parts.append("用户自述：\n" + "\n".join(
                f"- {s}" for s in self.profile["raw_sentences"]))
        recent = [m for m in self.history[-10:]
                  if m["role"] in ("user", "assistant")]
        if recent:
            context_parts.append("最近对话：\n" + "\n".join(
                ("用户" if m["role"] == "user" else "PI Agent")
                + "：" + m["content"][:200] for m in recent))
        context = "\n\n".join(context_parts) or "（暂无对话上下文）"

        body = self._kimi_once(
            SCRIPT_PROMPT,
            f"用户画像：\n{profile_txt}\n\n对话上下文：\n{context}",
            timeout=180)  # 长文输出，放宽超时
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"browse_script_{ts}.md"
        content = (
            "# 10 分钟网页浏览剧本\n\n"
            f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            "> 依据：用户画像 + 对话上下文，由 Kimi 生成\n\n"
            + body + "\n")
        (self.notes_dir / fname).write_text(content, encoding="utf-8")
        (self.notes_dir / "browse_script.md").write_text(  # 最新一份的快捷入口
            content, encoding="utf-8")
        self._log("PI Agent", f"浏览剧本已生成：{fname}")
        return fname

    def _latest_script(self) -> str:
        latest = self.notes_dir / "browse_script.md"
        if latest.exists():
            return latest.read_text(encoding="utf-8")
        if not self.api_key:
            return "⚠️ 未配置 Kimi API Key，无法生成剧本。"
        if not self.profile:
            return "⚠️ 还没有画像，先聊两句让我认识你，剧本会自动生成。"
        return "⏳ 剧本还在后台生成中，稍等几秒再输入「剧本」查看。"

    @staticmethod
    def _render_profile_md(d: dict) -> str:
        raw = "\n".join(f"- {s}" for s in d.get("raw_sentences", [])) or "（无）"
        refinements = "\n".join(
            f"- {r['time']}：{r['note']}" for r in d.get("refinements", [])
        ) or "（暂无）"
        return f"""# 用户画像

> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}
> 生成方式：根据用户原始语句由 Kimi 分析生成，并在对话中持续微调

## 用户原始语句

{raw}

## 画像分析

### 基本信息

- 昵称：{d.get("nickname", "")}
- 年龄段：{d.get("age", "")}

### 性格

- 性格标签：{d.get("personality_tags", "") or "（无）"}
- 性格描述：{d.get("personality_text", "") or "（无）"}

### 需求与价值取向

- 价值标签：{d.get("value_tags", "") or "（无）"}
- 价值描述：{d.get("value_text", "") or "（无）"}

### 其他

{d.get("extra", "") or "（无）"}

## 画像微调记录

{refinements}
"""

    def _profile_summary(self) -> str:
        """把画像浓缩成一段文字，注入 Kimi 的系统提示词。"""
        if not self.profile:
            return ""
        p = self.profile
        return (
            "\n\n当前用户画像：\n"
            f"- 昵称：{p.get('nickname', '未知')}，年龄段：{p.get('age', '未知')}\n"
            f"- 性格：{p.get('personality_tags', '')}；{p.get('personality_text', '')}\n"
            f"- 需求与价值取向：{p.get('value_tags', '')}；{p.get('value_text', '')}\n"
            f"- 补充：{p.get('extra', '')}\n"
            "请根据以上画像调整你的语气、解释深度和推荐方向。"
        )

    # ---------- Markdown 指令实现 ----------

    def _write(self, name: str, content: str) -> str:
        p = self._safe_path(name)
        p.write_text(content + "\n", encoding="utf-8")
        return f"✅ 已写入 `{p.name}`（{len(content)} 个字符）"

    def _append(self, name: str, content: str) -> str:
        p = self._safe_path(name)
        with p.open("a", encoding="utf-8") as f:
            f.write(content + "\n")
        return f"✅ 已追加到 `{p.name}`"

    def _read(self, name: str) -> str:
        p = self._safe_path(name)
        if not p.exists():
            return f"⚠️ 文件 `{p.name}` 不存在，先用「写 {p.name} 内容」创建它吧。"
        return f"📄 `{p.name}` 的内容：\n\n{p.read_text(encoding='utf-8')}"

    def _list(self) -> str:
        files = sorted(f.name for f in self.notes_dir.glob("*.md"))
        if not files:
            return "📂 notes/ 里还没有任何 .md 文件。"
        return "📂 现有 Markdown 文件：\n" + "\n".join(f"- {n}" for n in files)

    # ---------- Kimi for Coding API ----------

    def _kimi_once(self, system: str, user: str, timeout: int = 60) -> str:
        """单轮调用（不占用对话上下文），失败抛异常。"""
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "PI-Agent/1.0",  # 如实声明客户端身份
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()

    def _chat_with_kimi(self, msg: str) -> str:
        """交互阶段对话：多轮上下文 + 画像个性化 + 关卡剧情角色。"""
        self.history.append({"role": "user", "content": msg})
        self.history = self.history[-20:]  # 最多保留 20 条上下文
        system = SYSTEM_PROMPT + self._profile_summary()
        # 剧情模式下：自由对话保持「未来的自己」角色，不推进剧情
        if self.story_active and not self.story_done:
            node = STORY_TREE[self.story_node]
            system += STORY_ROLE_PROMPT.format(
                emoji=node["emoji"], title=node["title"], desc=node["desc"])
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
                        + self.history,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "PI-Agent/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            answer = data["choices"][0]["message"]["content"].strip()
            self.history.append({"role": "assistant", "content": answer})
            return answer
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            self.history.pop()  # 失败的消息不留进上下文
            return f"❌ Kimi API 返回 {e.code}：{detail}"
        except Exception as e:
            self.history.pop()
            return f"❌ 调用 Kimi API 失败：{e}"

    # ---------- 对外入口（交互阶段） ----------

    def reply(self, message: str) -> str:
        msg = message.strip()
        if not msg:
            return "说点什么吧～"
        self._log("你", msg)
        history_len = len(self.history)
        answer = self._handle(msg)
        self._log("PI Agent", answer)
        # 非剧情模式：根据用户语句动态切换画面（注意：场景 0 也是合法值）
        if not self.story_active:
            matched = self._match_scene(msg)
            if matched is not None:
                self.current_scene = matched
        # 每轮真实对话后，后台检查画像是否需要微调
        if len(self.history) > history_len:
            self._refine_profile_async()
            # 每两轮对话换一张根据新内容生成的背景
            self._round_count += 1
            if self._round_count % 2 == 0 and self.api_key:
                threading.Thread(target=self._gen_background_safe,
                                 daemon=True).start()
        return answer

    def _handle(self, msg: str) -> str:
        m = re.match(r"^(读|read)\s+(\S+)$", msg, re.I)
        if m:
            return self._read(m.group(2))

        m = re.match(r"^(写|write)\s+(\S+)\s+([\s\S]+)$", msg, re.I)
        if m:
            return self._write(m.group(2), m.group(3).strip())

        m = re.match(r"^(追加|append)\s+(\S+)\s+([\s\S]+)$", msg, re.I)
        if m:
            return self._append(m.group(2), m.group(3).strip())

        if msg.lower() in ("列表", "ls", "list"):
            return self._list()

        if msg.lower() in ("清空上下文", "reset"):
            self.history.clear()
            return "🧹 对话上下文已清空。"

        if msg in ("剧本", "浏览剧本", "script"):
            return self._latest_script()

        if msg in ("生成剧本", "重做剧本"):
            if not self.profile:
                return "⚠️ 还没有画像，先聊两句让我认识你。"
            try:
                fname = self.generate_browse_script()
                return f"✅ 已根据最新画像重新生成剧本：`{fname}`\n输入「剧本」查看内容。"
            except Exception as e:
                return f"❌ 剧本生成失败：{e}"

        # 其余消息交给 Kimi 大模型
        if self.api_key:
            return self._chat_with_kimi(msg)

        return (
            "⚠️ 还没有配置 Kimi API Key，我只能做 Markdown 读写。\n\n"
            "配置方法（二选一）：\n"
            "1. 启动前设置环境变量：`export KIMI_API_KEY=sk-kimi-...`\n"
            "2. 把 Key 写进项目根目录的 `kimi_key.txt` 文件\n\n"
            "配置好后我就能真正智能对话了。现在仍可试试：\n"
            "- `写 hello.md # 你好`\n- `读 hello.md`\n- `列表`"
        )
