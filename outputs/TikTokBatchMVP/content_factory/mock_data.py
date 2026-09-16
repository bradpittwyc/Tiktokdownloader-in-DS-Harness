"""本地演示数据生成。

用途：UI 已经全部做出来，但真实闭环需要 TikTok 登录态与网络。
这个模块让「内容流水线 / AI 加工 / 首页 / 异常处理」在没有真实数据时也能
完整演示，且**不伪造真实结果** —— 生成的数据用 `source_type="demo"` 标记，
界面上会显示「演示数据」标签，一键即可清除。

它写的是同一套 sqlite 表、同一套字段，所以真实数据接进来之后
（source_type="tiktok"）两者在界面上表现完全一致，页面结构不用改。
"""

import random
import time

from .factory_store import new_id

DEMO_SOURCE = "demo"

SAMPLES = [
    {
        "video_id": "demo_7312000000000000001",
        "handle": "emilyintech",
        "name": "Emily Zhang",
        "title": "3 habits that changed my life as a software engineer",
        "description": "Sharing the three daily habits that made the biggest difference in my career.",
        "duration": 37,
        "thumbnail": "",
        "transcript": ("I used to think productivity was about doing more, but I learned the hard way that "
                       "it's actually about protecting your focus. The first habit is what I call a "
                       "shutdown ritual. Every evening I write down the three things that actually matter "
                       "for tomorrow, and then I close my laptop. That single habit cut my anxiety in half. "
                       "The second one is batching shallow work. Email, Slack, small reviews, they all go "
                       "into one thirty-minute block in the afternoon. The third habit is probably the "
                       "hardest one to keep. I schedule a two-hour deep work block before anyone else on "
                       "my team is awake."),
        "enrichment": {
            "topic": "职场成长", "subtopic": "效率与习惯", "cefr_level": "B1", "accent": "美音",
            "speech_speed": "中等", "learning_value": 0.89,
            "keywords": ["productivity", "focus", "shutdown ritual", "deep work", "batch"],
            "expressions": [
                {"text": "learned the hard way", "meaning_zh": "吃了苦头才明白",
                 "example": "I learned the hard way that sleep matters."},
                {"text": "protect your focus", "meaning_zh": "保护自己的专注力"},
                {"text": "cut my anxiety in half", "meaning_zh": "让我的焦虑减半"},
                {"text": "batching shallow work", "meaning_zh": "把琐碎工作集中处理"},
            ],
            "grammar_points": ["一般过去时与现在完成时对比", "what I call + 名词短语", "before 引导时间状语从句"],
            "key_sentences": [
                {"text": "I used to think productivity was about doing more.",
                 "translation_zh": "我以前以为效率就是做更多的事。"},
                {"text": "That single habit cut my anxiety in half.",
                 "translation_zh": "就这一个习惯让我的焦虑减少了一半。"},
                {"text": "I schedule a two-hour deep work block before anyone else on my team is awake.",
                 "translation_zh": "我会在团队里任何人醒来之前安排两小时的深度工作。"},
            ],
            "summary_zh": "作者分享作为软件工程师改变她生活的三个习惯：关机仪式、批量处理琐事、以及早起深度工作。",
            "recommended_task": "跟读",
        },
    },
    {
        "video_id": "demo_7312000000000000002",
        "handle": "techwithtim",
        "name": "Tim / Tech & Life",
        "title": "The real reason AI will change everything",
        "description": "It is not about robots taking jobs. It is about the cost of thinking going to zero.",
        "duration": 72,
        "transcript": ("Everyone asks me whether AI is going to take our jobs, and honestly that is the wrong "
                       "question. The real shift is that the cost of thinking is collapsing. When something "
                       "becomes a thousand times cheaper, you do not just do the same thing more cheaply, "
                       "you do entirely new things. Think about what happened when computing became cheap. "
                       "Nobody in 1970 predicted that we would carry a supercomputer in our pocket and use "
                       "it mostly to look at pictures of cats. So when I look at AI, I do not see a "
                       "replacement. I see a tool that lets one person do the work of a small team."),
        "enrichment": {
            "topic": "AI 科技", "subtopic": "AI 与就业", "cefr_level": "B2", "accent": "美音",
            "speech_speed": "偏快", "learning_value": 0.92,
            "keywords": ["collapse", "shift", "replacement", "supercomputer", "predict"],
            "expressions": [
                {"text": "that is the wrong question", "meaning_zh": "这个问题问错了"},
                {"text": "the cost of thinking is collapsing", "meaning_zh": "思考的成本正在崩塌式下降"},
                {"text": "do the work of a small team", "meaning_zh": "干完一个小团队的活"},
            ],
            "grammar_points": ["whether 引导宾语从句", "when 引导时间状语从句 + 过去将来时", "not...just... 强调结构"],
            "key_sentences": [
                {"text": "When something becomes a thousand times cheaper, you do not just do the same thing more cheaply, you do entirely new things.",
                 "translation_zh": "当某样东西便宜一千倍时，你不只是更便宜地做同样的事，而是会做全新的事。"},
                {"text": "I see a tool that lets one person do the work of a small team.",
                 "translation_zh": "我看到的是一个让一个人能完成小团队工作的工具。"},
            ],
            "summary_zh": "作者认为 AI 的关键不是取代工作，而是「思考成本」急剧下降，会让个人具备小团队的产出。",
            "recommended_task": "复述",
        },
    },
    {
        "video_id": "demo_7312000000000000003",
        "handle": "aliabdaal",
        "name": "Ali Abdaal",
        "title": "How to stay productive (without burnout)",
        "description": "The three-step framework I use to stay productive without burning out.",
        "duration": 58,
        "transcript": ("Burnout does not come from working too hard. It comes from working hard on things that "
                       "do not feel like they matter. So the first step is to audit your energy, not your "
                       "time. Write down everything you did yesterday and mark each item as either "
                       "energising or draining. The second step is to protect the energising ones fiercely "
                       "and delegate or delete the draining ones. And the third step, which people always "
                       "skip, is to schedule rest before you need it, not after you crash."),
        "enrichment": {
            "topic": "效率", "subtopic": "精力管理", "cefr_level": "B1", "accent": "英音",
            "speech_speed": "中等", "learning_value": 0.86,
            "keywords": ["burnout", "audit", "energising", "draining", "delegate"],
            "expressions": [
                {"text": "burnout does not come from working too hard", "meaning_zh": "倦怠不是因为工作太拼"},
                {"text": "audit your energy, not your time", "meaning_zh": "审视你的精力，而不是时间"},
                {"text": "schedule rest before you need it", "meaning_zh": "在需要休息之前就安排休息"},
            ],
            "grammar_points": ["not...but... 对比结构", "which 引导非限制性定语从句", "祈使句"],
            "key_sentences": [
                {"text": "It comes from working hard on things that do not feel like they matter.",
                 "translation_zh": "它来自拼命做那些感觉并不重要的事。"},
                {"text": "Schedule rest before you need it, not after you crash.",
                 "translation_zh": "在需要休息之前就安排休息，而不是等你垮掉之后。"},
            ],
            "summary_zh": "三步法避免倦怠：审视精力而非时间、保护充电事项、提前安排休息。",
            "recommended_task": "影子跟读",
        },
    },
    {
        "video_id": "demo_7312000000000000004",
        "handle": "genzfinaance",
        "name": "Gen Z Finance",
        "title": "This is what nobody tells you about freelancing",
        "description": "The unglamorous truth about going freelance.",
        "duration": 45,
        "transcript": ("Nobody tells you that freelancing is mostly sales. You can be the best designer in "
                       "the world, but if you cannot find the next client, you are out of business. The "
                       "second thing nobody tells you is that your income becomes lumpy. You might make "
                       "ten thousand dollars in March and nothing in April. So the rule I follow is simple. "
                       "Live on last month's money and keep six months of expenses in cash."),
        "enrichment": {
            "topic": "商业", "subtopic": "自由职业", "cefr_level": "B1", "accent": "美音",
            "speech_speed": "中等", "learning_value": 0.83,
            "keywords": ["freelancing", "lumpy", "client", "expenses", "in cash"],
            "expressions": [
                {"text": "you are out of business", "meaning_zh": "你就没生意可做了"},
                {"text": "your income becomes lumpy", "meaning_zh": "你的收入变得忽高忽低"},
                {"text": "live on last month's money", "meaning_zh": "靠上个月赚的钱生活"},
            ],
            "grammar_points": ["if 条件状语从句", "might 表可能性", "the rule I follow 定语从句省略 that"],
            "key_sentences": [
                {"text": "You can be the best designer in the world, but if you cannot find the next client, you are out of business.",
                 "translation_zh": "你可能是世界上最好的设计师，但如果找不到下一个客户，你就没生意可做。"},
                {"text": "Live on last month's money and keep six months of expenses in cash.",
                 "translation_zh": "靠上个月的钱生活，并保留六个月开销的现金。"},
            ],
            "summary_zh": "自由职业的真相：本质是销售、收入不稳定，因此要用上月收入生活并留足现金。",
            "recommended_task": "复述",
        },
    },
    {
        "video_id": "demo_7312000000000000005",
        "handle": "devlife",
        "name": "Dev Life",
        "title": "A day in my life as a software engineer",
        "description": "A realistic look at a normal working day.",
        "duration": 63,
        "transcript": ("Six thirty in the morning, coffee first, then a quick review of what I shipped "
                       "yesterday. Stand-up at nine, and honestly stand-up is my favourite meeting because "
                       "it is fifteen minutes and then everyone disappears to actually work. The afternoon "
                       "is where the real engineering happens. No meetings, headphones on, and I do not "
                       "come out until the feature works."),
        "enrichment": {
            "topic": "编程", "subtopic": "日常工作", "cefr_level": "A2", "accent": "美音",
            "speech_speed": "中等", "learning_value": 0.74,
            "keywords": ["stand-up", "ship", "headphones", "feature", "meeting"],
            "expressions": [
                {"text": "stand-up is my favourite meeting", "meaning_zh": "站会是我最喜欢的会议"},
                {"text": "everyone disappears to actually work", "meaning_zh": "大家都消失去真正干活了"},
                {"text": "headphones on", "meaning_zh": "戴上耳机"},
            ],
            "grammar_points": ["一般现在时描述日常", "where 引导定语从句", "until 引导时间状语从句"],
            "key_sentences": [
                {"text": "The afternoon is where the real engineering happens.",
                 "translation_zh": "下午才是真正写代码的时间。"},
                {"text": "I do not come out until the feature works.",
                 "translation_zh": "功能没跑通我就不出来。"},
            ],
            "summary_zh": "一名软件工程师的普通一天：早起看昨天进度、九点站会、下午专注写代码。",
            "recommended_task": "跟读",
        },
    },
    {
        "video_id": "demo_7312000000000000006",
        "handle": "englishwithlucy",
        "name": "English with Lucy",
        "title": "Why you should learn English with stories",
        "description": "Why stories beat word lists.",
        "duration": 52,
        "transcript": ("Here is the thing about vocabulary lists. You memorise fifty words, and by Friday "
                       "you remember maybe six of them. But if those same fifty words appear inside a story "
                       "you actually care about, your brain files them under meaning instead of under "
                       "homework. That is why I always tell my students to read one page they love rather "
                       "than ten pages they tolerate."),
        "enrichment": {
            "topic": "教育", "subtopic": "英语学习法", "cefr_level": "B1", "accent": "英音",
            "speech_speed": "中等", "learning_value": 0.95,
            "keywords": ["vocabulary", "memorise", "file under", "tolerate", "care about"],
            "expressions": [
                {"text": "here is the thing about", "meaning_zh": "关于……是这样的"},
                {"text": "your brain files them under meaning", "meaning_zh": "你的大脑把它们归档到「意义」下面"},
                {"text": "one page they love rather than ten pages they tolerate", "meaning_zh": "读一页他们喜欢的，而不是十页他们忍受的"},
            ],
            "grammar_points": ["祈使句 + and 结构", "if 引导条件句 + 一般现在时", "rather than 对比"],
            "key_sentences": [
                {"text": "If those same fifty words appear inside a story you actually care about, your brain files them under meaning instead of under homework.",
                 "translation_zh": "如果这五十个词出现在你真正在意的故事里，你的大脑会把它们归档到「意义」而不是「作业」下面。"},
                {"text": "Read one page they love rather than ten pages they tolerate.",
                 "translation_zh": "读一页他们喜爱的，而不是十页他们忍受的。"},
            ],
            "summary_zh": "背单词表效率低，把词放进你在意的故事里，大脑才会按意义记住它们。",
            "recommended_task": "阅读拓展",
        },
    },
    {
        "video_id": "demo_7312000000000000007",
        "handle": "hubbermanlab",
        "name": "Huberman Lab",
        "title": "The future of remote work",
        "description": "What the data says about remote work.",
        "duration": 88,
        "transcript": ("The data on remote work is more nuanced than either side admits. For focused "
                       "individual work, remote wins almost every time. For creative collaboration, the "
                       "evidence tilts the other way, because the best ideas often come from accidental "
                       "conversations in a hallway. So the future is probably not remote or office. It is "
                       "a deliberate mix, and the teams that win will be the ones that choose on purpose "
                       "instead of copying whatever the loudest company does."),
        "enrichment": None,        # 故意留待分析，界面初始就有「待分析」状态
    },
    {
        "video_id": "demo_7312000000000000008",
        "handle": "jordanbpetersen",
        "name": "Jordan B Peterson",
        "title": "This changed my mindset",
        "description": "A short talk on mindset.",
        "duration": 135,
        "transcript": ("You are not going to fix your life in one afternoon. What you can do is fix one "
                       "small thing today, and then keep that small thing fixed. That is how order gets "
                       "built, incrementally, and it is also how confidence gets built, because every "
                       "small promise you keep to yourself is evidence that your word means something."),
        "enrichment": None,
    },
    {
        "video_id": "demo_7312000000000000009",
        "handle": "melrobbins",
        "name": "Mel Robbins",
        "title": "Simple habits for a better you",
        "description": "Small habits, big change.",
        "duration": 45,
        "transcript": ("Your habits are not going to change because you feel motivated. They change because "
                       "you make them stupidly easy. If you want to read more, put the book on your pillow "
                       "in the morning. Motivation is a feeling, and feelings are terrible project managers."),
        "enrichment": None,
    },
    {
        "video_id": "demo_7312000000000000010",
        "handle": "colinandsamir",
        "name": "Colin and Samir",
        "title": "Why most people never achieve their goals",
        "description": "The real reason goals fail.",
        "duration": 52,
        "transcript": ("Most people do not fail because they lack ambition. They fail because their goal "
                       "lives in their head instead of on their calendar. A goal without a scheduled block "
                       "is not a goal, it is a wish, and wishes do not survive a busy week."),
        "enrichment": None,
    },
]

# 演示用的失败记录：让「异常处理」页有真实结构的内容可看（明确标注为演示）
FAILED_SAMPLES = [
    {"handle": "moonvy", "title": "Design system walkthrough", "reason": "网络请求超时（Connection Timeout）"},
    {"handle": "andreikarpathy", "title": "How transformers actually work", "reason": "API 请求超时（504）"},
]


def seed_demo_items(store, reset=False):
    """写入演示数据。返回写入统计。重复调用不会产生重复行。"""
    if reset:
        clear_demo_items(store)
    created = 0
    for sample in SAMPLES:
        before = store.find_item_by_source(DEMO_SOURCE, sample["video_id"])
        item_id = store.upsert_item(
            sample["video_id"], source_type=DEMO_SOURCE,
            source_url=f"https://www.tiktok.com/@{sample['handle']}/video/{sample['video_id'].split('_')[-1]}",
            creator_handle=sample["handle"], creator_name=sample["name"],
            title=sample["title"], description=sample["description"],
            duration=sample["duration"], thumbnail_path=sample.get("thumbnail", ""),
            transcript_text=sample["transcript"],
            download_status="done", transcript_status="done",
            ai_status="pending",
        )
        if not before:
            created += 1
        if sample["enrichment"]:
            store.save_enrichment(item_id, sample["enrichment"],
                                  raw_json=sample["enrichment"],
                                  raw_response="（本地演示数据，未调用真实模型）",
                                  attempts=1, model="demo-data")
    for index, failure in enumerate(FAILED_SAMPLES, 1):
        video_id = f"demo_73120000000000001{index:02d}"
        item_id = store.upsert_item(
            video_id, source_type=DEMO_SOURCE,
            source_url=f"https://www.tiktok.com/@{failure['handle']}/video/{video_id}",
            creator_handle=failure["handle"], creator_name=failure["handle"],
            title=failure["title"], description="",
            duration=60, download_status="done", transcript_status="done",
            transcript_text="(demo transcript)",
        )
        store.set_ai_status(item_id, "failed", failure["reason"])
    store.upsert_creator("emilyintech", display_name="Emily Zhang", category="AI 科技",
                         priority="高", poll_interval="1 小时", followers=48200, videos=132)
    store.upsert_creator("techwithtim", display_name="Tim / Tech & Life", category="AI 科技",
                         priority="高", poll_interval="1 小时", followers=126000, videos=412)
    store.upsert_creator("aliabdaal", display_name="Ali Abdaal", category="效率", priority="高",
                         poll_interval="30 分钟", followers=880000, videos=612)
    store.upsert_creator("englishwithlucy", display_name="English with Lucy", category="教育",
                         priority="高", poll_interval="1 小时", followers=340000, videos=289)
    for failure in FAILED_SAMPLES:
        store.upsert_creator(failure["handle"], display_name=failure["handle"],
                             category="设计", status="error", priority="中")
    store.upsert_creator("moonvy", display_name="Moonvy", category="设计", status="error")
    store.upsert_creator("andreikarpathy", display_name="Andrej Karpathy", category="AI 研究",
                         status="error")
    return {"ok": True, "created": created, "total": len(SAMPLES) + len(FAILED_SAMPLES),
            "demo": True, "seeded_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def clear_demo_items(store):
    """删掉所有演示数据。limit 给足，避免只删掉第一页。"""
    removed = 0
    for row in store.items(limit=5000):
        if row.get("source_type") == DEMO_SOURCE:
            store.delete_item(row["id"])
            removed += 1
    return {"ok": True, "removed": removed}
