# AI 标注质量对比报告（Prompt A/B）

生成时间：2026-09-16 11:16:35

## 一、这次比的是什么

- 样本：**20 条**固定字幕（`tests/fixtures/ai_benchmark/items.json`）
- 模型：`deepseek-chat`（服务商 DeepSeek）
- Prompt A（baseline）：`ai-enrichment-v1`
- Prompt B（tuned）：`ai-enrichment-v2`

**两个 Prompt 的核心差异**

- A：基线版本。硬性要求 expressions 3–6 条、grammar_points 2–4 条，会逼模型为凑数量提普通词与教科书式语法点。
- B：调优版本。① 数量不设下限、允许空数组，禁止为凑数提普通词与教科书语法；② expressions 明确优先级与排除项；③ key_sentences 必须逐字来自字幕；④ learning_value 给出四档锚点；⑤ CEFR 要求综合判断并说明依据；⑥ 拿不准的口音/语速填「不确定」，不许靠猜。

## 二、结论（总分）

- A 总分：**0.89**
- B 总分：**0.92**
- 变化：**+0.03** → **新版更好**

## 三、七个维度逐项对比

| 维度 | A（baseline） | B（tuned） | 变化 | 说明 |
|---|---|---|---|---|
| 主题判断准确度 | 0.97 | 0.97 | +0.00 | 基本持平 |
| CEFR 难度合理性 | 1.00 | 1.00 | +0.00 | 基本持平 |
| 重点表达质量 | 0.80 | 0.82 | +0.01 | 基本持平 |
| 语法点质量 | 0.53 | 0.74 | +0.21 | 语法点更精、凑数更少 |
| 重点句质量 | 0.98 | 0.98 | +0.00 | 基本持平 |
| 学习价值打分准确度 | 0.98 | 1.00 | +0.02 | 学习价值打分更准 |
| 没有编造、有据可依 | 0.97 | 0.97 | -0.01 | 基本持平 |

> 分数范围 0–1，越高越好。评分方法见文末「评分是怎么算的」。

## 四、为什么是这个分数（辅助指标）

这些指标不参与总分，但能说清改进到底来自哪里 ——比如「提得更准了」和「只是提得更多了」是两件完全不同的事。

| 指标 | A（baseline） | B（tuned） | 变化 |
|---|---|---|---|
| 提出的表达里真正值得教的比例 | 0.87 | 0.91 | +0.04 |
| 人工标注的必提表达被提到了多少 | 0.72 | 0.69 | -0.03 |
| 语法点里纯术语凑数的比例（越低越好） | 0.48 | 0.00 | -0.48 |
| 提出的表达条数（均值） | 5.25 | 4.85 | -0.40 |
| 语法点条数（均值） | 3.75 | 2.80 | -0.95 |

> `expression_precision` 越高说明提出的表达越是真值得教的；`expression_recall` 越高说明越少漏掉人工认定的必提表达。两者常常此消彼长 —— 新版如果提得更少但更准，precision 升、recall 降，这本身不是坏事，但需要你来判断取舍。

### 值得注意的几点（自动挑出，供你判断）

- 语法点凑数明显减少：纯术语（如「一般现在时」）占比从 48% 降到 0%；平均条数从 3.75 条降到 2.8 条。
- 表达更准但更少：真正值得教的比例从 87% 升到 91%，但人工认定的必提表达召回从 72% 降到 69% —— 新版更保守，会漏掉一些人工认为值得教的表达。
- 两版没有差别的维度：主题判断准确度、CEFR 难度合理性、重点表达质量、重点句质量、没有编造、有据可依 —— 说明这些方面旧 Prompt 已经做得不错。
- 有 1 条内容新版反而更低分，例如：The metaverse hype, tw。逐条对照见下一节。
- 有 13 条内容新版更高分。

## 五、逐条内容对照

### ML engineer explains how models actually get deployed

`ai-tech-interview-01`　类别：访谈

- 总分：A 0.93 → B 0.97（+0.04）
- 具体变化：
  - 新版新增表达：the easy part、half the time
  - 新版不再提取：latency budgets、feature pipelines
  - 语法点从 4 条减到 3 条
  - 学习价值 0.85 → 0.80
- 重点表达：A 5 条 / B 5 条（质量分 0.68 → 0.92）
- 语法点：A 4 条 / B 3 条（质量分 0.83 → 0.88）

### The real reason AI will change everything

`ai-tech-opinion-02`　类别：AI / 科技

- 总分：A 0.96 → B 0.95（-0.01）
- 具体变化：
  - 新版新增表达：a thousand times cheaper
  - 新版不再提取：the wrong question、entirely new things
  - 语法点从 4 条减到 3 条
- 重点表达：A 5 条 / B 4 条（质量分 0.90 → 0.80）
- 语法点：A 4 条 / B 3 条（质量分 0.83 → 0.88）

### The metaverse hype, two years later

`ai-tech-skeptic-09`　类别：AI / 科技

- 总分：A 0.98 → B 0.89（-0.09）
- 具体变化：
  - 新版新增表达：fizzle out、get ahead of
  - 新版不再提取：fizzled out、got ahead of the substance
  - 语法点从 4 条减到 3 条
  - 难度判断 B2 → C1
  - 学习价值 0.82 → 0.80
  - 重点句 5 条 → 3 条
  - 主题 科技评论 → 科技趋势
- 重点表达：A 5 条 / B 5 条（质量分 1.00 → 0.60）
- 语法点：A 4 条 / B 3 条（质量分 0.83 → 0.88）

### Fine-tuning vs prompting, explained simply

`ai-tech-tutorial-08`　类别：教程

- 总分：A 0.91 → B 0.96（+0.05）
- 具体变化：
  - 新版新增表达：Let's start with the basics、The rule of thumb is、And one more thing
  - 新版不再提取：The rule of thumb、house style、dramatically cheaper
  - 语法点从 4 条减到 3 条
  - 学习价值 0.82 → 0.75
- 重点表达：A 6 条 / B 6 条（质量分 0.84 → 0.84）
- 语法点：A 4 条 / B 3 条（质量分 0.55 → 0.88）

### How to write copy that actually converts

`business-copywriting-05`　类别：商业

- 总分：A 0.80 → B 0.87（+0.07）
- 具体变化：
  - 新版新增表达：Here's the thing about、name the pain
  - 新版不再提取：Here's the thing、synergy-driven
  - 语法点从 4 条减到 3 条
  - 重点句 4 条 → 5 条
- 重点表达：A 5 条 / B 5 条（质量分 0.72 → 0.92）
- 语法点：A 4 条 / B 3 条（质量分 0.36 → 0.75）

### Nobody tells you this about freelancing

`business-freelance-03`　类别：商业

- 总分：A 0.89 → B 0.95（+0.05）
- 具体变化：
  - 新版新增表达：the rule I follow is simple
  - 新版不再提取：lumpy、live on last month's money、the rule I follow
  - 注意：新版把表达写长了（更像整句片段、不好迁移）：the rule I follow is simple
  - 语法点从 4 条减到 3 条
  - 学习价值 0.82 → 0.80
  - 重点句 5 条 → 3 条
- 重点表达：A 5 条 / B 3 条（质量分 0.80 → 0.76）
- 语法点：A 4 条 / B 3 条（质量分 0.46 → 0.88）

### Why most startups fail in year two

`business-startup-06`　类别：商业

- 总分：A 0.89 → B 0.90（+0.00）
- 具体变化：
  - 新版新增表达：mistake A for B、chase a market that didn't exist yet、shrink month after month
  - 新版不再提取：seed round、runway、chasing a market、mistook A for B
  - 语法点从 4 条减到 3 条
  - 学习价值 0.85 → 0.80
  - 主题 职场成长 → 创业商业
- 重点表达：A 6 条 / B 5 条（质量分 0.80 → 0.64）
- 语法点：A 4 条 / B 3 条（质量分 0.46 → 0.75）

### 3 habits that changed my life as a software engineer

`career-habits-04`　类别：职场

- 总分：A 0.98 → B 1.00（+0.02）
- 具体变化：
  - 新版新增表达：the hardest one to keep
  - 新版不再提取：deep work block
  - 语法点从 4 条减到 3 条
  - 难度判断 B1 → B2
  - 学习价值 0.85 → 0.80
  - 重点句 5 条 → 4 条
- 重点表达：A 6 条 / B 6 条（质量分 1.00 → 1.00）
- 语法点：A 4 条 / B 3 条（质量分 0.83 → 1.00）

### The salary negotiation line that works

`career-negotiation-07`　类别：职场

- 总分：A 0.89 → B 0.93（+0.04）
- 具体变化：
  - 新版不再提取：talk about the number
  - 语法点从 3 条减到 2 条
  - 学习价值 0.85 → 0.75
- 重点表达：A 6 条 / B 5 条（质量分 1.00 → 1.00）
- 语法点：A 3 条 / B 2 条（质量分 0.38 → 0.62）

### POV: your Monday morning

`daily-lowinfo-20`　类别：日常口语

- 总分：A 0.77 → B 0.93（+0.16）
- 具体变化：
  - 新版不再提取：POV、feel the same way
  - 新版判断这条没什么语法值得讲，返回空数组
  - 学习价值 0.40 → 0.20
  - 重点句 4 条 → 3 条
- 重点表达：A 4 条 / B 2 条（质量分 0.50 → 0.50）
- 语法点：A 3 条 / B 0 条（质量分 0.27 → 1.00）

### Coffee shop small talk that sounds natural

`daily-smalltalk-16`　类别：日常口语

- 总分：A 0.87 → B 0.87（+0.00）
- 具体变化：
  - 新版新增表达：how's your week been、can't complain、same here、get on 等 6 条
  - 新版不再提取：How's your week been?、Can't complain、Same here、I should let you get on、Let's grab a coffee sometime、See you around
  - 语法点从 4 条减到 3 条
  - 学习价值 0.85 → 0.70
- 重点表达：A 6 条 / B 6 条（质量分 1.00 → 0.93）
- 语法点：A 4 条 / B 3 条（质量分 0.20 → 0.27）

### How I stopped overthinking everything

`growth-anxiety-14`　类别：个人成长

- 总分：A 0.85 → B 0.93（+0.08）
- 具体变化：
  - 新版新增表达：the trap
  - 新版不再提取：the spiral is gone、stupidly simple
  - 语法点从 4 条减到 2 条
  - 难度判断 B2 → B1
  - 学习价值 0.85 → 0.75
  - 重点句 5 条 → 4 条
  - 主题 心理健康 → 心理成长
- 重点表达：A 5 条 / B 4 条（质量分 0.56 → 0.61）
- 语法点：A 4 条 / B 2 条（质量分 0.46 → 1.00）

### Small habits, big change

`growth-habits-15`　类别：个人成长

- 总分：A 0.85 → B 0.95（+0.10）
- 具体变化：
  - 新版新增表达：make terrible project managers
  - 新版不再提取：beat motivation、workout clothes、terrible project managers
  - 语法点从 4 条减到 3 条
  - 学习价值 0.90 → 0.75
  - 重点句 3 条 → 4 条
- 重点表达：A 4 条 / B 2 条（质量分 0.58 → 0.87）
- 语法点：A 4 条 / B 3 条（质量分 0.64 → 0.75）

### This changed my mindset

`growth-mindset-13`　类别：个人成长

- 总分：A 0.93 → B 0.93（+0.00）
- 具体变化：
  - 新版新增表达：fix one small thing、keep a promise to yourself
  - 新版不再提取：fix your life、every small promise you keep to yourself
  - 语法点从 4 条减到 3 条
  - 难度判断 B1 → B2
- 重点表达：A 5 条 / B 5 条（质量分 0.84 → 0.76）
- 语法点：A 4 条 / B 3 条（质量分 0.64 → 0.75）

### Interview: what nobody says about success

`interview-honest-17`　类别：访谈

- 总分：A 0.97 → B 0.99（+0.02）
- 具体变化：
  - 新版新增表达：get wrong about、far more mundane than that、a series of、the cumulative effect of 等 5 条
  - 新版不再提取：get wrong、cumulative effect、uncomfortable corollary
  - 语法点从 3 条增到 3 条
  - 重点句 5 条 → 4 条
- 重点表达：A 4 条 / B 6 条（质量分 0.80 → 0.93）
- 语法点：A 3 条 / B 3 条（质量分 1.00 → 1.00）

### The only pasta sauce you need

`lifestyle-cooking-12`　类别：教程

- 总分：A 0.85 → B 0.89（+0.04）
- 具体变化：
  - 新版新增表达：a good glug of、in goes the tomatoes
  - 新版不再提取：a good glug of olive oil
  - 语法点从 3 条增到 4 条
  - 学习价值 0.75 → 0.72
  - 重点句 5 条 → 3 条
  - 主题 美食烹饪 → 生活技巧
- 重点表达：A 5 条 / B 6 条（质量分 0.84 → 0.92）
- 语法点：A 3 条 / B 4 条（质量分 0.13 → 0.30）

### My 5am morning routine, honestly

`lifestyle-morning-10`　类别：生活方式

- 总分：A 0.86 → B 0.89（+0.03）
- 具体变化：
  - 新版新增表达：get up、sit down with
  - 新版不再提取：get up at five、start the day、needs something from me
  - 语法点从 3 条增到 3 条
  - 学习价值 0.72 → 0.62
  - 重点句 5 条 → 3 条
- 重点表达：A 6 条 / B 5 条（质量分 0.80 → 0.80）
- 语法点：A 3 条 / B 3 条（质量分 0.27 → 0.40）

### Living in a 20 square meter apartment

`lifestyle-tinyhome-11`　类别：生活方式

- 总分：A 0.89 → B 0.93（+0.04）
- 具体变化：
  - 新版新增表达：it turns out、nearly as much as you think
  - 新版不再提取：It turns out (that)...、not nearly as much as、living small
  - 语法点从 4 条减到 3 条
  - 学习价值 0.80 → 0.75
  - 重点句 5 条 → 4 条
- 重点表达：A 6 条 / B 5 条（质量分 0.80 → 0.78）
- 语法点：A 4 条 / B 3 条（质量分 0.55 → 0.88）

### Why you should learn English with stories

`tutorial-english-learning-18`　类别：教程

- 总分：A 0.89 → B 0.91（+0.02）
- 具体变化：
  - 新版新增表达：you actually care about、instead of
  - 新版不再提取：care about
  - 语法点从 4 条减到 3 条
  - 学习价值 0.90 → 0.80
  - 主题 英语学习 → 英语学习方法
- 重点表达：A 5 条 / B 6 条（质量分 0.60 → 0.72）
- 语法点：A 4 条 / B 3 条（质量分 0.74 → 0.75）

### Phone photography: fix your lighting first

`tutorial-photo-lighting-19`　类别：教程

- 总分：A 0.87 → B 0.88（+0.01）
- 具体变化：
  - 新版新增表达：you are already ahead of ninety percent of people、shooting indoors、diffuse it with a white curtain、It flattens everything
  - 新版不再提取：you are already ahead of、diffuse it with、flattens everything、makes skin look plastic
  - 注意：新版把表达写长了（更像整句片段、不好迁移）：you are already ahead of ninety percent of people、diffuse it with a white curtain
  - 语法点从 4 条减到 3 条
  - 学习价值 0.85 → 0.75
- 重点表达：A 6 条 / B 6 条（质量分 1.00 → 1.00）
- 语法点：A 4 条 / B 3 条（质量分 0.20 → 0.13）

---

## 六、评分是怎么算的（重要）

评分**不是让模型给自己打分**，而是拿模型的输出与人工标注的参考答案比对，外加「有没有出处」的硬检查：

- **主题判断准确度**：命中人工认可的主题给满分；主题错了但子主题说到了要点给一半。
- **CEFR 难度合理性**：落在人工给的难度区间内满分，差一档给一半，差两档给 0.2。
- **重点表达质量**：一条表达要同时满足「真的出自字幕」和「不是单个普通词」才算有效；再与人工列出的必提表达比对，衡量有没有漏掉真正该教的。
- **语法点质量**：宁可少而准。这条内容本来没什么语法可讲时，返回空数组是**满分**；出现「一般现在时」这类教科书式凑数条目要扣分。
- **重点句质量**：必须逐字出自字幕（允许截取连续片段，不允许改写），长度适中、带中文翻译的得分更高。
- **学习价值打分准确度**：模型给的分数落在人工区间内算准，偏离越远越低。
- **有据可依程度**：expressions 与 key_sentences 里有多少能在字幕里找到出处；字幕极短却写出一大段具体摘要也要扣分。

这套评分是启发式的，用来**做相对比较**（A 比 B 好还是差），不适合当成绝对质量分。原始模型输出都在 `baseline.json` / `tuned.json` 里，可以逐条复核。
