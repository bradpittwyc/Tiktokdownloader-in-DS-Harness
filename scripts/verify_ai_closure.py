"""用真实 API Key 把 AI 标注闭环端到端跑一遍（会真的联网调模型）。

这是人工验证工具，不进测试套件（测试必须离线）。用法：

    python scripts/verify_ai_closure.py            # 用现有配置，跑 3 条
    python scripts/verify_ai_closure.py --limit 5
    python scripts/verify_ai_closure.py --item <item_id>
    python scripts/verify_ai_closure.py --reuse-learning   # 从 learning.json 导入 api_key

做的事：
1. 准备一条有字幕文本的待标注内容（演示数据里的样例，带真实英文 transcript）；
2. 调 settings 里的服务商 / 模型 / API Key，真的发一次请求；
3. 打印原始返回、解析后的 12 个字段、耗时与重试次数；
4. 写进本地 sqlite，再从库里读回来确认「重启后仍在」；
5. 失败时打印可读的原因（不吞异常）。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "outputs" / "TikTokBatchMVP"
sys.path.insert(0, str(BASE))

from content_factory.ai_enrichment import EnrichmentError, EnrichmentService  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.pipeline import ContentPipeline  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402


def import_learning_key(settings):
    """把旧的 learning.json 里的 api_key / api_base / model 搬进内容工厂设置。

    用户其实已经在学习文档功能里配过 DeepSeek，没必要让人再填一遍。
    """
    path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokBatchMVP" / "learning.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    key = str(data.get("api_key") or "").strip()
    if not key:
        return None
    settings.update("ai", {
        "api_key": key,
        "api_base": data.get("api_base") or settings.get("ai", "api_base"),
        "model": data.get("model") or settings.get("ai", "model"),
    })
    return {"api_base": data.get("api_base"), "model": data.get("model")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--item", default="")
    parser.add_argument("--reuse-learning", action="store_true",
                        help="从 %LOCALAPPDATA%/TikTokBatchMVP/learning.json 导入 api_key")
    parser.add_argument("--transcript", default="")
    args = parser.parse_args()

    settings = FactorySettings()
    store = FactoryStore()
    pipeline = ContentPipeline(store, settings, emit=lambda name, payload:
                               print(f"   [事件] {name} {json.dumps(payload, ensure_ascii=False)[:150]}"))

    if args.reuse_learning:
        imported = import_learning_key(settings)
        print(f"已从 learning.json 导入配置：{imported}")
    if args.transcript and not args.item:
        item_id = store.upsert_item(
            f"verify-{int(time.time())}", source_type="local",
            title="验证用内容（手工字幕）", creator_handle="verify",
            download_status="done", transcript_status="done", transcript_text=args.transcript)
        ids = [item_id]
    elif args.item:
        ids = [args.item]
    else:
        from content_factory.mock_data import seed_demo_items
        seed_demo_items(store)
        ids = [row["id"] for row in store.items(limit=500)
               if row["ai_status"] in ("pending", "failed")][:args.limit]

    config = EnrichmentService(settings).config()
    print(f"服务商 {config['provider']} / 模型 {config['model']} / 地址 {config['api_base']}")
    print(f"API Key：{'已配置 ' + config['api_key'][:6] + '...' + config['api_key'][-4:] if config['api_key'] else '未配置'}")
    if not config["api_key"]:
        print("没有 API Key：请先在「设置 → AI 加工设置」填写，或用 --reuse-learning 从 learning.json 导入")
        return 2
    print(f"待标注 {len(ids)} 条\n")

    # 先单独验一次连通性，避免把网络问题和解析问题混在一起
    probe = EnrichmentService(settings).test_connection()
    print(f"连通性测试：{probe}\n")
    if not probe.get("ok"):
        return 3

    ok_count = 0
    for index, item_id in enumerate(ids, 1):
        item = store.item(item_id)
        if not item:
            print(f"[{index}] 内容不存在：{item_id}")
            continue
        print(f"[{index}/{len(ids)}] {item['title'][:60]}  (字幕 {len(item.get('transcript_text') or '')} 字符)")
        started = time.time()
        result = pipeline.enrich_one(item_id)
        elapsed = round(time.time() - started, 1)
        if not result.get("ok"):
            print(f"   失败（{elapsed}s）：{result.get('error')}")
            continue
        ok_count += 1
        saved = result["enrichment"]
        print(f"   成功（{elapsed}s，尝试 {result['attempts']} 次）")
        for field in ("topic", "subtopic", "cefr_level", "accent", "speech_speed",
                      "learning_value", "recommended_task"):
            print(f"     {field}: {saved.get(field)}")
        print(f"     keywords({len(saved['keywords'])}): {saved['keywords']}")
        print(f"     expressions({len(saved['expressions'])}): "
              f"{[e.get('text') for e in saved['expressions']]}")
        print(f"     grammar_points({len(saved['grammar_points'])}): {saved['grammar_points']}")
        print(f"     key_sentences({len(saved['key_sentences'])}): "
              f"{[s.get('text', '')[:40] for s in saved['key_sentences']]}")
        print(f"     summary_zh: {saved['summary_zh'][:110]}")
        print(f"     raw_response 长度 {len(saved.get('raw_response') or '')}")

        # 从库里重新读一遍：确认真的落盘（= 重启后还在）
        reopened = FactoryStore().item_view(item_id)
        print(f"     落盘校验：ai_status={reopened['ai_status']} "
              f"topic={reopened['enrichment']['topic']} model={reopened['enrichment']['model']}")
        print()

    print(f"完成：{ok_count}/{len(ids)} 条成功")
    return 0 if ok_count else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnrichmentError as exc:
        print("标注失败：", exc)
        raise SystemExit(1)
