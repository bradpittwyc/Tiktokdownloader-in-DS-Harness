"""AI 标注质量基准 runner：同一批内容分别用 Prompt A / B 跑一遍并出对比报告。

用法
----
    # 两个 Prompt 各跑一遍（默认 v1 基线 vs v2 调优）
    python scripts/run_ai_benchmark.py

    # 只跑某几条 / 换输出目录 / 换版本
    python scripts/run_ai_benchmark.py --only ai-tech-opinion-02,daily-lowinfo-20
    python scripts/run_ai_benchmark.py --out benchmark-results --a ai-enrichment-v1 --b ai-enrichment-v2

    # 不联网自检：只验证数据能读、评分能算、报告能生成
    python scripts/run_ai_benchmark.py --dry-run

API Key 从哪来
--------------
1. 优先用内容工厂自己的设置（%LOCALAPPDATA%/TikTokBatchMVP/content-factory-settings.json）
2. 其次读环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY
3. 都没有就明确报错退出 —— 不会静默产出空报告

**绝不**把 Key 写进代码、fixture 或报告。报告里只记录模型名与服务商。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "outputs" / "TikTokBatchMVP"))

from content_factory.ai_enrichment import EnrichmentError, EnrichmentService  # noqa: E402
from content_factory.benchmark import (  # noqa: E402
    item_inputs, load_benchmark, score_result)
from content_factory.benchmark_report import build_comparison, write_reports  # noqa: E402
from content_factory.prompts import PROMPT_LIBRARY, get_prompt  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

ENV_KEYS = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "AI_API_KEY")


class OverlaySettings:
    """在真实设置之上叠加临时值，不写盘。

    为什么需要它：benchmark 要指定用哪个 prompt 版本，但绝不能顺手把用户的
    设置改掉（跑完一次对比，用户的默认 prompt 被换了，这是很讨厌的事）。
    这里只做只读叠加：section() 先看临时值，再回落到真实设置。
    """

    def __init__(self, base, overlay):
        self._base = base
        self._overlay = dict(overlay or {})

    def section(self, name):
        merged = dict(self._base.section(name))
        merged.update(self._overlay.get(name, {}))
        return merged

    def get(self, section, key, default=None):
        return self.section(section).get(key, default)

    def update(self, *_args, **_kwargs):        # pragma: no cover - 明确禁止写盘
        raise RuntimeError("benchmark 不允许修改用户设置")

    def save(self, *_args, **_kwargs):          # pragma: no cover
        raise RuntimeError("benchmark 不允许修改用户设置")


def resolve_credentials(settings):
    """返回 (api_key, 来源说明)。绝不打印 Key 本身。"""
    key = str(settings.section("ai").get("api_key") or "").strip()
    if key:
        return key, "内容工厂设置（设置 → AI 加工设置）"
    for name in ENV_KEYS:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value, f"环境变量 {name}"
    return "", ""


def run_version(service, items, version, limit=None, verbose=True):
    """用指定 prompt 版本跑完整个 Benchmark。"""
    spec = get_prompt(version)
    if spec is None:
        raise SystemExit(f"未知的 prompt 版本：{version}（可选：{', '.join(PROMPT_LIBRARY)}）")
    records = []
    for index, item in enumerate(items[:limit] if limit else items, 1):
        started = time.time()
        record = {"id": item["id"], "title": item["title"],
                  "category": item.get("category", ""), "version": version}
        try:
            result, raw, payload, attempts = service.enrich(item_inputs(item), item["transcript"])
            record["result"] = result
            record["attempts"] = attempts
            record["elapsed"] = round(time.time() - started, 2)
            record["raw_response"] = raw
            record["scores"] = score_result(result, item["expect"], item["transcript"])
            if verbose:
                print(f"  [{index:2d}/{len(items)}] {item['id']:30s} "
                      f"总分 {record['scores']['overall']:.2f}  "
                      f"表达 {len(result.get('expressions') or [])} 条 / "
                      f"语法 {len(result.get('grammar_points') or [])} 条  "
                      f"({record['elapsed']}s)")
        except EnrichmentError as exc:
            record["error"] = str(exc)
            if verbose:
                print(f"  [{index:2d}/{len(items)}] {item['id']:30s} 失败：{exc}")
        except Exception as exc:                       # 兜底，不让一条挂掉整轮
            record["error"] = f"{type(exc).__name__}: {exc}"
            if verbose:
                print(f"  [{index:2d}/{len(items)}] {item['id']:30s} 异常：{exc}")
        records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser(description="AI 标注质量基准（Prompt A/B 对比）")
    parser.add_argument("--a", default="ai-enrichment-v1", help="baseline 版本")
    parser.add_argument("--b", default="ai-enrichment-v2", help="tuned 版本")
    parser.add_argument("--out", default="benchmark-results", help="结果目录")
    parser.add_argument("--only", default="", help="只跑指定 id，逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--model", default="", help="临时覆盖模型名")
    parser.add_argument("--dry-run", action="store_true",
                        help="不联网：只验证数据、评分与报告生成")
    parser.add_argument("--suffix", default="", help="给结果文件加后缀，便于多次运行对比")
    args = parser.parse_args()

    data = load_benchmark()
    items = data["items"]
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        items = [item for item in items if item["id"] in wanted]
        missing = wanted - {item["id"] for item in items}
        if missing:
            raise SystemExit(f"Benchmark 里没有这些 id：{sorted(missing)}")
    if args.limit:
        items = items[:args.limit]
    print(f"Benchmark：{len(items)} 条（{Path(data['path']).name}）")

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO / out_dir
    suffix = f"-{args.suffix}" if args.suffix else ""

    if args.dry_run:
        print("\n--dry-run：不调用模型，只用空结果验证评分与报告链路")
        fake = [{"id": item["id"], "title": item["title"], "category": item.get("category", ""),
                 "result": {}, "scores": score_result({}, item["expect"], item["transcript"])}
                for item in items]
        comparison = build_comparison(fake, fake, {"model": "dry-run", "provider": "dry-run",
                                                   "version_a": args.a, "version_b": args.b})
        json_path, md_path = write_reports(comparison, out_dir, {
            "a": get_prompt(args.a).notes if get_prompt(args.a) else "",
            "b": get_prompt(args.b).notes if get_prompt(args.b) else ""})
        print(f"已生成（dry-run）：{json_path}\n已生成（dry-run）：{md_path}")
        return 0

    settings = FactorySettings()
    key, source = resolve_credentials(settings)
    if not key:
        print("找不到 API Key，无法运行基准测试。\n"
              "  方式一：在应用里打开「设置 → AI 加工设置」填写 API Key"
              "（已经配过下载器的，可点「导入已有配置」）\n"
              "  方式二：设置环境变量 " + " / ".join(ENV_KEYS) + "\n"
              "  （若只想验证数据与报告链路，可加 --dry-run）")
        return 2
    print(f"凭据来源：{source}")

    ai_section = dict(settings.section("ai"))
    ai_section["api_key"] = key
    if args.model:
        ai_section["model"] = args.model

    results = {}
    for label, version in (("baseline", args.a), ("tuned", args.b)):
        spec = get_prompt(version)
        if spec is None:
            raise SystemExit(f"未知版本：{version}")
        print(f"\n=== {label}（{version}）===")
        overlay = OverlaySettings(settings, {"ai": dict(ai_section, prompt_template=version)})
        service = EnrichmentService(overlay)
        config = service.config()
        if args.model:
            print(f"  模型覆盖为 {config['model']}")
        results[label] = run_version(service, items, version)

    out_dir.mkdir(parents=True, exist_ok=True)
    for label, records in results.items():
        path = out_dir / f"{label}{suffix}.json"
        path.write_text(json.dumps({"version": args.a if label == "baseline" else args.b,
                                    "model": EnrichmentService(
                                        OverlaySettings(settings, {"ai": ai_section})).config()["model"],
                                    "records": records}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"\n已保存 {path}")

    comparison = build_comparison(
        results["baseline"], results["tuned"],
        {"model": EnrichmentService(OverlaySettings(settings, {"ai": ai_section})).config()["model"],
         "provider": settings.section("ai").get("provider", ""),
         "version_a": args.a, "version_b": args.b,
         "fixture": Path(data["path"]).name, "items": len(items)})
    if suffix:
        comparison["meta"]["suffix"] = args.suffix
    json_path, md_path = write_reports(comparison, out_dir, {
        "a": get_prompt(args.a).notes if get_prompt(args.a) else "",
        "b": get_prompt(args.b).notes if get_prompt(args.b) else ""})

    overall = comparison["summary"].get("overall", {})
    print(f"\n已保存 {json_path}")
    print(f"已保存 {md_path}")
    if overall.get("delta") is not None:
        print(f"\n总分：A {overall['baseline']:.2f} → B {overall['tuned']:.2f} "
              f"（{overall['delta']:+.2f}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
