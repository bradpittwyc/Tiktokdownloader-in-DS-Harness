"""故意制造失败，验证「失败原因能不能说清楚」（会真的联网）。

为什么必须实测：成功路径验证不了失败路径的知识。模型名写错、Key 无效、
额度用尽、返回空内容、返回非 JSON —— 这些在真实使用里都会遇到，
而界面上只显示 last_error 这一行字。如果这一行是 "HTTPError: 400"，
用户完全不知道该怎么办。

用法：
    python scripts/verify_ai_failures.py --reuse-learning
    python scripts/verify_ai_failures.py --reuse-learning --only bad-model
"""
import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "outputs" / "TikTokBatchMVP"
sys.path.insert(0, str(BASE))

from content_factory.ai_enrichment import EnrichmentError, EnrichmentService  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.pipeline import ContentPipeline  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

TRANSCRIPT = ("I used to think productivity was about doing more, but I learned the hard way "
              "that it is actually about protecting your focus. Every evening I write down "
              "the three things that matter for tomorrow, and then I close my laptop.")


class ScriptedHttp:
    """给「网络层没出错、但返回内容有问题」这类场景用的假 HTTP。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        reply = self.replies[min(self.calls - 1, len(self.replies) - 1)]

        class Response:
            def raise_for_status(self):
                return None

            def json(self_inner):
                return reply

        return Response()


def fresh_item(store, tag):
    return store.upsert_item(
        f"failcheck-{tag}", source_type="local",
        title=f"失败路径验证：{tag}", creator_handle="verify",
        download_status="done", transcript_status="done", transcript_text=TRANSCRIPT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-learning", action="store_true")
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    settings = FactorySettings()
    store = FactoryStore()
    pipeline = ContentPipeline(store, settings, emit=lambda *_: None)

    if args.reuse_learning:
        legacy = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                  / "TikTokBatchMVP" / "learning.json")
        data = json.loads(legacy.read_text(encoding="utf-8"))
        settings.update("ai", {"api_key": data.get("api_key", ""),
                               "api_base": data.get("api_base", ""),
                               "model": data.get("model", "")})
    good = EnrichmentService(settings).config()
    if not good["api_key"]:
        print("没有可用 API Key，先 --reuse-learning 或去设置里填")
        return 2
    print(f"基准配置：{good['provider']} / {good['model']} / {good['api_base']}\n")

    scenarios = []

    # 1) 模型名写错 —— 真实 HTTP 400
    cases = {
        "bad-model": lambda: settings.update("ai", {"model": "deepseek-chat-nonexistent"}),
        "bad-key": lambda: settings.update("ai", {"api_key": "sk-definitely-invalid-key"}),
        "bad-base": lambda: settings.update("ai", {"api_base": "https://api.deepseek.com/v1-nope"}),
    }
    for name, mutate in cases.items():
        if args.only and args.only != name:
            continue
        mutate()
        item_id = fresh_item(store, name)
        scenarios.append((name, pipeline.enrich_one(item_id), item_id))
        # 还原
        settings.update("ai", {"model": good["model"], "api_key": good["api_key"],
                               "api_base": good["api_base"]})

    # 2) 网络层正常但内容不对：空回复 / 非 JSON / 一直非 JSON
    fake_cases = [
        ("empty-content", [{"choices": [{"message": {"content": ""}}]}]),
        ("prose-not-json", [{"choices": [{"message": {"content": "抱歉，我无法分析这段内容。"}}]}] * 2),
        ("broken-json", [{"choices": [{"message": {"content": '{"topic": "AI 科技", "cefr'}}]}] * 2),
        ("missing-choices", [{"error": {"message": "rate limit exceeded"}}] * 2),
    ]
    for name, replies in fake_cases:
        if args.only and args.only != name:
            continue
        service = EnrichmentService(settings, http=ScriptedHttp(replies))
        pipeline._enricher = service
        item_id = fresh_item(store, name)
        scenarios.append((name, pipeline.enrich_one(item_id), item_id))
        pipeline._enricher = EnrichmentService(settings)

    print("=" * 78)
    for name, result, item_id in scenarios:
        row = store.item(item_id)
        status = row["ai_status"]
        message = row["last_error"] or (result.get("error") or "")
        flag = "OK " if (not result.get("ok") and message) else "!! "
        print(f"{flag}{name:16s} ai_status={status:7s} 失败原因：{message[:150]}")
        if result.get("ok"):
            print("   （这条居然成功了，说明该失败场景没有生效）")
    print("=" * 78)
    print("\n关注点：每条失败都必须 (1) 状态为 failed，(2) 有一句人能看懂的原因。")

    # 收尾：把验证用的临时记录删掉，别污染内容库（重复跑也不会越堆越多）
    removed = 0
    for row in store.items(limit=1000):
        if str(row.get("source_video_id") or "").startswith("failcheck-"):
            store.delete_item(row["id"])
            removed += 1
    print(f"已清理验证用临时记录 {removed} 条（内容库现有 {store.counts()['total']} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
