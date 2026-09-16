"""用当前评分器给已保存的 A/B 结果重新打分（不调用模型）。

用途：改了评分规则之后，不必重新花钱跑一遍模型 —— results 里存着两轮的完整
原始输出，直接重算即可。这也是"评分可比较"的前提：评分规则变了，
旧结果的分数必须能一起重算，而不是新旧混着比。
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "outputs" / "TikTokBatchMVP"))

from content_factory.benchmark import SCORE_FIELDS, load_benchmark, score_result  # noqa: E402
from content_factory.benchmark_report import (build_comparison, render_markdown,  # noqa: E402
                                              write_reports)
from content_factory.prompts import get_prompt  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="benchmark-results")
    parser.add_argument("--a", default="baseline")
    parser.add_argument("--b", default="tuned")
    args = parser.parse_args()

    out_dir = Path(args.dir)
    if not out_dir.is_absolute():
        out_dir = REPO / out_dir

    data = load_benchmark()
    items = {item["id"]: item for item in data["items"]}

    def rescore(label):
        payload = json.loads((out_dir / f"{label}.json").read_text(encoding="utf-8"))
        for record in payload["records"]:
            item = items.get(record["id"])
            if not item or not record.get("result"):
                continue
            record["scores"] = score_result(record["result"], item["expect"], item["transcript"])
        return payload

    base = rescore(args.a)
    tuned = rescore(args.b)
    comparison = build_comparison(
        base["records"], tuned["records"],
        {"model": base.get("model", ""), "provider": "DeepSeek",
         "version_a": base.get("version", ""), "version_b": tuned.get("version", ""),
         "fixture": Path(data["path"]).name, "items": len(items),
         "note": "分数由当前评分器对已保存的原始输出重算"})

    a_notes = get_prompt(base.get("version", ""))
    b_notes = get_prompt(tuned.get("version", ""))
    json_path, md_path = write_reports(comparison, out_dir, {
        "a": a_notes.notes if a_notes else "", "b": b_notes.notes if b_notes else ""})

    print(f"重算完成：{json_path}")
    print(f"重算完成：{md_path}\n")
    summary = comparison["summary"]
    print(f"{'维度':28s} {'A':>6s} {'B':>6s} {'变化':>7s}")
    for field in SCORE_FIELDS:
        entry = summary[field]
        delta = entry["delta"]
        print(f"{entry['label']:28s} {entry['baseline']:6.2f} {entry['tuned']:6.2f} "
              f"{delta:+7.2f}")
    overall = summary["overall"]
    print(f"{'总分':28s} {overall['baseline']:6.2f} {overall['tuned']:6.2f} "
          f"{overall['delta']:+7.2f}")


if __name__ == "__main__":
    main()
