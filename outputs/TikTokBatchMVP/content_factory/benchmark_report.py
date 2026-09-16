"""A/B 对比报告生成（纯计算，不联网，可单测）。

产出两份东西给两种读者：
- comparison.json：给机器/后续分析用，含逐条分数与差异
- comparison.md  ：给**不看代码的人**用 —— 每条内容左右对照、说清「变了什么」、
                  哪个维度变好/变差，最后给一句人话结论

报告里必须带足够的方法说明（评分怎么算、样本几条、用了哪个模型），
否则读者无法判断这些数字能不能信。
"""

import json
from datetime import datetime
from pathlib import Path

from .benchmark import EXTRA_METRICS, SCORE_FIELDS, SCORE_LABELS, _normalise

# 人话解释：分数变化时该怎么描述
DIRECTION_WORDS = {
    "topic_accuracy": ("主题判断更准", "主题判断变差"),
    "cefr_quality": ("难度判断更接近人工标注", "难度判断偏离人工标注"),
    "expression_quality": ("重点表达更值得学", "重点表达质量下降"),
    "grammar_quality": ("语法点更精、凑数更少", "语法点变得更像凑数"),
    "key_sentence_quality": ("重点句选得更合适", "重点句选得更差"),
    "learning_value_quality": ("学习价值打分更准", "学习价值打分更偏"),
    "hallucination": ("更少编造、更贴原文", "编造/脱离原文变多"),
}

# 分数变化的判定阈值：小于这个幅度算「持平」，避免把噪声说成提升
FLAT_THRESHOLD = 0.02


def _fmt(value):
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _texts(entries, key="text"):
    return [str((entry or {}).get(key) or "").strip()
            for entry in (entries or []) if str((entry or {}).get(key) or "").strip()]


def build_comparison(records_a, records_b, meta=None):
    """把两轮结果对比成结构化数据。

    records_* 形如：
        [{"id":..., "title":..., "result": {...12 字段...}, "scores": {...}},
         {"id":..., "error": "..."}]
    """
    meta = dict(meta or {})
    by_id_a = {row["id"]: row for row in records_a}
    by_id_b = {row["id"]: row for row in records_b}
    rows = []
    for item_id in sorted(set(by_id_a) | set(by_id_b)):
        row_a, row_b = by_id_a.get(item_id), by_id_b.get(item_id)
        base = row_a or row_b or {}
        scores_a = (row_a or {}).get("scores")
        scores_b = (row_b or {}).get("scores")
        delta = {}
        if scores_a and scores_b:
            delta = {field: round(scores_b[field] - scores_a[field], 4)
                     for field in SCORE_FIELDS}
            delta["overall"] = round(scores_b["overall"] - scores_a["overall"], 4)
        rows.append({
            "id": item_id,
            "title": base.get("title", ""),
            "category": base.get("category", ""),
            "error_a": (row_a or {}).get("error", ""),
            "error_b": (row_b or {}).get("error", ""),
            "scores_a": scores_a or {},
            "scores_b": scores_b or {},
            "delta": delta,
            "result_a": (row_a or {}).get("result") or {},
            "result_b": (row_b or {}).get("result") or {},
        })

    summary = {}
    for field in list(SCORE_FIELDS) + ["overall"]:
        values_a = [row["scores_a"][field] for row in rows if row["scores_a"]]
        values_b = [row["scores_b"][field] for row in rows if row["scores_b"]]
        mean_a = round(sum(values_a) / len(values_a), 4) if values_a else None
        mean_b = round(sum(values_b) / len(values_b), 4) if values_b else None
        summary[field] = {
            "baseline": mean_a,
            "tuned": mean_b,
            "delta": round(mean_b - mean_a, 4) if (mean_a is not None and mean_b is not None) else None,
            "label": SCORE_LABELS.get(field, field),
        }
    summary["_failures"] = {
        "baseline": sum(1 for row in rows if row["error_a"]),
        "tuned": sum(1 for row in rows if row["error_b"]),
    }
    # 辅助指标：解释"分数为什么是这样"，不参与总分
    for field in EXTRA_METRICS:
        values_a = [row["scores_a"][field] for row in rows
                    if row["scores_a"] and row["scores_a"].get(field) is not None]
        values_b = [row["scores_b"][field] for row in rows
                    if row["scores_b"] and row["scores_b"].get(field) is not None]
        mean_a = round(sum(values_a) / len(values_a), 4) if values_a else None
        mean_b = round(sum(values_b) / len(values_b), 4) if values_b else None
        summary[field] = {
            "baseline": mean_a,
            "tuned": mean_b,
            "delta": round(mean_b - mean_a, 4) if (mean_a is not None and mean_b is not None) else None,
            "label": EXTRA_METRICS[field],
        }
    for key, label in (("expression_total", "提出的表达条数（均值）"),
                       ("grammar_total", "语法点条数（均值）")):
        values_a = [row["scores_a"][key] for row in rows
                    if row["scores_a"] and row["scores_a"].get(key) is not None]
        values_b = [row["scores_b"][key] for row in rows
                    if row["scores_b"] and row["scores_b"].get(key) is not None]
        mean_a = round(sum(values_a) / len(values_a), 2) if values_a else None
        mean_b = round(sum(values_b) / len(values_b), 2) if values_b else None
        summary[key] = {
            "baseline": mean_a, "tuned": mean_b,
            "delta": round(mean_b - mean_a, 2) if (mean_a is not None and mean_b is not None) else None,
            "label": label,
        }
    summary["_count"] = len(rows)
    return {"meta": meta, "rows": rows, "summary": summary,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def _changed_lines(row):
    """用一句人话说明这一条到底变了什么。"""
    lines = []
    a, b = row["result_a"], row["result_b"]
    texts_a, texts_b = _texts(a.get("expressions")), _texts(b.get("expressions"))
    only_a = [text for text in texts_a if text not in texts_b]
    only_b = [text for text in texts_b if text not in texts_a]
    if only_b:
        shown = "、".join(only_b[:4])
        suffix = f" 等 {len(only_b)} 条" if len(only_b) > 4 else ""
        lines.append(f"新版新增表达：{shown}{suffix}")
    if only_a:
        shown = "、".join(only_a[:6])
        suffix = f" 等 {len(only_a)} 条" if len(only_a) > 6 else ""
        lines.append(f"新版不再提取：{shown}{suffix}")

    # 表达变长不一定是好事：把整句片段当"表达"提出来，学习者没法迁移使用。
    # 实测出现过："diffuse it with" 被扩成 "diffuse it with a white curtain"。
    grew = [text for text in only_b
            if len(text.split()) >= 6 and any(
                len(text.split()) > len(other.split())
                and _normalise(text).startswith(_normalise(other))
                for other in only_a)]
    if grew:
        lines.append(f"注意：新版把表达写长了（更像整句片段、不好迁移）：{'、'.join(grew[:3])}")

    grammar_a = [str(x) for x in (a.get("grammar_points") or [])]
    grammar_b = [str(x) for x in (b.get("grammar_points") or [])]
    if grammar_a != grammar_b:
        if not grammar_b:
            lines.append("新版判断这条没什么语法值得讲，返回空数组")
        elif len(grammar_b) < len(grammar_a):
            lines.append(f"语法点从 {len(grammar_a)} 条减到 {len(grammar_b)} 条")
        else:
            lines.append(f"语法点从 {len(grammar_a)} 条增到 {len(grammar_b)} 条")

    if str(a.get("cefr_level") or "") != str(b.get("cefr_level") or ""):
        lines.append(f"难度判断 {a.get('cefr_level') or '—'} → {b.get('cefr_level') or '—'}")
    if a.get("learning_value") != b.get("learning_value"):
        lines.append(f"学习价值 {_fmt(a.get('learning_value'))} → {_fmt(b.get('learning_value'))}")
    sentences_a = len(_texts(a.get("key_sentences")))
    sentences_b = len(_texts(b.get("key_sentences")))
    if sentences_a != sentences_b:
        lines.append(f"重点句 {sentences_a} 条 → {sentences_b} 条")
    if str(a.get("topic") or "") != str(b.get("topic") or ""):
        lines.append(f"主题 {a.get('topic') or '—'} → {b.get('topic') or '—'}")
    return lines


def build_highlights(comparison):
    """自动挑出几条值得人看的观察，避免读者自己在 20 条里翻。

    只做"摆事实"式的观察，不下"哪个更好"的结论 —— 取舍应该由人来做。
    """
    summary = comparison["summary"]
    rows = comparison["rows"]
    notes = []

    filler = summary.get("grammar_filler_ratio") or {}
    if filler.get("delta") is not None and filler["delta"] < -0.05:
        notes.append(
            f"语法点凑数明显减少：纯术语（如「一般现在时」）占比从 "
            f"{filler['baseline']:.0%} 降到 {filler['tuned']:.0%}；"
            f"平均条数从 {summary.get('grammar_total', {}).get('baseline')} 条降到 "
            f"{summary.get('grammar_total', {}).get('tuned')} 条。")

    precision = summary.get("expression_precision") or {}
    recall = summary.get("expression_recall") or {}
    if precision.get("delta") is not None and recall.get("delta") is not None:
        if precision["delta"] > 0.01 and recall["delta"] < -0.01:
            notes.append(
                f"表达更准但更少：真正值得教的比例从 {precision['baseline']:.0%} 升到 "
                f"{precision['tuned']:.0%}，但人工认定的必提表达召回从 "
                f"{recall['baseline']:.0%} 降到 {recall['tuned']:.0%} —— "
                "新版更保守，会漏掉一些人工认为值得教的表达。")
        elif precision["delta"] > 0.01:
            notes.append(f"表达更准：真正值得教的比例从 {precision['baseline']:.0%} "
                         f"升到 {precision['tuned']:.0%}，召回没有下降。")

    unchanged = [summary[field]["label"] for field in SCORE_FIELDS
                 if (summary.get(field) or {}).get("delta") is not None
                 and abs(summary[field]["delta"]) <= FLAT_THRESHOLD]
    if unchanged:
        notes.append("两版没有差别的维度：" + "、".join(unchanged)
                     + " —— 说明这些方面旧 Prompt 已经做得不错。")

    worse = [row for row in rows if row["delta"] and row["delta"].get("overall", 0) < -0.02]
    if worse:
        names = "、".join((row["title"] or row["id"])[:22] for row in worse[:3])
        notes.append(f"有 {len(worse)} 条内容新版反而更低分，例如：{names}。"
                     "逐条对照见下一节。")

    better = [row for row in rows if row["delta"] and row["delta"].get("overall", 0) > 0.02]
    if better:
        notes.append(f"有 {len(better)} 条内容新版更高分。")
    return notes


def render_markdown(comparison, prompt_notes=None):
    """生成给非程序员看的对比报告。"""
    meta = comparison["meta"]
    summary = comparison["summary"]
    rows = comparison["rows"]
    out = []
    out.append("# AI 标注质量对比报告（Prompt A/B）\n\n")
    out.append(f"生成时间：{comparison['generated_at']}\n\n")

    out.append("## 一、这次比的是什么\n\n")
    out.append(f"- 样本：**{summary['_count']} 条**固定字幕"
               "（`tests/fixtures/ai_benchmark/items.json`）\n")
    out.append(f"- 模型：`{meta.get('model', '—')}`（服务商 {meta.get('provider', '—')}）\n")
    out.append(f"- Prompt A（baseline）：`{meta.get('version_a', '—')}`\n")
    out.append(f"- Prompt B（tuned）：`{meta.get('version_b', '—')}`\n")
    if prompt_notes:
        out.append("\n**两个 Prompt 的核心差异**\n\n")
        out.append(f"- A：{prompt_notes.get('a', '')}\n")
        out.append(f"- B：{prompt_notes.get('b', '')}\n")

    out.append("\n## 二、结论（总分）\n\n")
    overall = summary.get("overall", {})
    delta = overall.get("delta")
    if delta is None:
        out.append("两轮有效样本不足，无法比较。\n")
    else:
        verdict = ("**新版更好**" if delta > FLAT_THRESHOLD else
                   "**新版更差**" if delta < -FLAT_THRESHOLD else "两版基本持平")
        out.append(f"- A 总分：**{_fmt(overall['baseline'])}**\n")
        out.append(f"- B 总分：**{_fmt(overall['tuned'])}**\n")
        out.append(f"- 变化：**{delta:+.2f}** → {verdict}\n")
    failures = summary.get("_failures", {})
    if failures.get("baseline") or failures.get("tuned"):
        out.append(f"- 调用失败：A {failures.get('baseline', 0)} 条 / "
                   f"B {failures.get('tuned', 0)} 条\n")

    out.append("\n## 三、七个维度逐项对比\n\n")
    out.append("| 维度 | A（baseline） | B（tuned） | 变化 | 说明 |\n")
    out.append("|---|---|---|---|---|\n")
    for field in SCORE_FIELDS:
        entry = summary.get(field) or {}
        if entry.get("delta") is None:
            out.append(f"| {entry.get('label', field)} | — | — | — | 样本不足 |\n")
            continue
        good, bad = DIRECTION_WORDS.get(field, ("变好", "变差"))
        if entry["delta"] > FLAT_THRESHOLD:
            note = good
        elif entry["delta"] < -FLAT_THRESHOLD:
            note = bad
        else:
            note = "基本持平"
        out.append(f"| {entry.get('label', field)} | {_fmt(entry['baseline'])} | "
                   f"{_fmt(entry['tuned'])} | {entry['delta']:+.2f} | {note} |\n")
    out.append("\n> 分数范围 0–1，越高越好。评分方法见文末「评分是怎么算的」。\n")

    out.append("\n## 四、为什么是这个分数（辅助指标）\n\n")
    out.append("这些指标不参与总分，但能说清改进到底来自哪里 ——"
               "比如「提得更准了」和「只是提得更多了」是两件完全不同的事。\n\n")
    out.append("| 指标 | A（baseline） | B（tuned） | 变化 |\n")
    out.append("|---|---|---|---|\n")
    for field in list(EXTRA_METRICS) + ["expression_total", "grammar_total"]:
        entry = summary.get(field)
        if not entry or entry.get("delta") is None:
            continue
        out.append(f"| {entry['label']} | {_fmt(entry['baseline'])} | "
                   f"{_fmt(entry['tuned'])} | {entry['delta']:+.2f} |\n")
    out.append("\n> `expression_precision` 越高说明提出的表达越是真值得教的；"
               "`expression_recall` 越高说明越少漏掉人工认定的必提表达。"
               "两者常常此消彼长 —— 新版如果提得更少但更准，precision 升、recall 降，"
               "这本身不是坏事，但需要你来判断取舍。\n")

    highlights = build_highlights(comparison)
    if highlights:
        out.append("\n### 值得注意的几点（自动挑出，供你判断）\n\n")
        for note in highlights:
            out.append(f"- {note}\n")

    out.append("\n## 五、逐条内容对照\n")
    for row in rows:
        out.append(f"\n### {row['title'] or row['id']}\n\n")
        out.append(f"`{row['id']}`　类别：{row['category'] or '—'}\n")
        if row["error_a"] or row["error_b"]:
            if row["error_a"]:
                out.append(f"- A 调用失败：{row['error_a']}\n")
            if row["error_b"]:
                out.append(f"- B 调用失败：{row['error_b']}\n")
        if not row["delta"]:
            out.append("\n（本条缺一侧结果，无法比较）\n")
            continue
        out.append(f"\n- 总分：A {_fmt(row['scores_a'].get('overall'))} → "
                   f"B {_fmt(row['scores_b'].get('overall'))}"
                   f"（{row['delta'].get('overall', 0):+.2f}）\n")
        changed = _changed_lines(row)
        if changed:
            out.append("- 具体变化：\n")
            for line in changed:
                out.append(f"  - {line}\n")
        else:
            out.append("- 具体变化：两版输出基本一致\n")
        out.append(f"- 重点表达：A {len(row['result_a'].get('expressions') or [])} 条 / "
                   f"B {len(row['result_b'].get('expressions') or [])} 条"
                   f"（质量分 {_fmt(row['scores_a'].get('expression_quality'))} → "
                   f"{_fmt(row['scores_b'].get('expression_quality'))}）\n")
        out.append(f"- 语法点：A {len(row['result_a'].get('grammar_points') or [])} 条 / "
                   f"B {len(row['result_b'].get('grammar_points') or [])} 条"
                   f"（质量分 {_fmt(row['scores_a'].get('grammar_quality'))} → "
                   f"{_fmt(row['scores_b'].get('grammar_quality'))}）\n")

    out.append("\n---\n\n## 六、评分是怎么算的（重要）\n\n")
    out.append("评分**不是让模型给自己打分**，而是拿模型的输出与人工标注的参考答案比对，"
               "外加「有没有出处」的硬检查：\n\n")
    out.append("- **主题判断准确度**：命中人工认可的主题给满分；主题错了但子主题说到了要点给一半。\n")
    out.append("- **CEFR 难度合理性**：落在人工给的难度区间内满分，差一档给一半，差两档给 0.2。\n")
    out.append("- **重点表达质量**：一条表达要同时满足「真的出自字幕」和「不是单个普通词」"
               "才算有效；再与人工列出的必提表达比对，衡量有没有漏掉真正该教的。\n")
    out.append("- **语法点质量**：宁可少而准。这条内容本来没什么语法可讲时，"
               "返回空数组是**满分**；出现「一般现在时」这类教科书式凑数条目要扣分。\n")
    out.append("- **重点句质量**：必须逐字出自字幕（允许截取连续片段，不允许改写），"
               "长度适中、带中文翻译的得分更高。\n")
    out.append("- **学习价值打分准确度**：模型给的分数落在人工区间内算准，偏离越远越低。\n")
    out.append("- **有据可依程度**：expressions 与 key_sentences 里有多少能在字幕里找到出处；"
               "字幕极短却写出一大段具体摘要也要扣分。\n")
    out.append("\n这套评分是启发式的，用来**做相对比较**（A 比 B 好还是差），"
               "不适合当成绝对质量分。原始模型输出都在 `baseline.json` / `tuned.json` 里，"
               "可以逐条复核。\n")
    return "".join(out)


def write_reports(comparison, out_dir, prompt_notes=None):
    """写出 comparison.json 与 comparison.md，返回两个路径。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "comparison.json"
    md_path = target / "comparison.md"
    json_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(render_markdown(comparison, prompt_notes), encoding="utf-8")
    return json_path, md_path
