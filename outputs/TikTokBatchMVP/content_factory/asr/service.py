"""转写服务：字幕优先 → 本地 ASR → 外部 provider，并把结果写回内容库。

职责边界（这是 ASR Core 唯一对外的主入口）：

    本地视频 / 音频
      → 检查已有字幕（content_items.local_subtitle_path + 媒体旁边同名字幕）
      → 有字幕就直接读（零成本、最准）
      → 没有字幕才进 ASR（本机 faster-whisper / openai-whisper，其次外部 provider）
      → 得到 transcript
      → 写回已有 ContentItem / Store（transcript_text / transcript_status /
        last_error / local_subtitle_path）
      → 失败给出结构化错误（错误码 + 人话 + 是否可重试），并可重试

它不 import webview、不碰界面、不知道 bridge 的存在；进度通过一个 emit 回调出去，
形状与 `ContentPipeline._report` 一致（`{id, state, message, stage, ...}`），
所以界面上现有的「转写」阶段提示与失败态不需要任何改动。

状态机沿用内容库已有的 `content_items.transcript_status`：
    pending → running → done | failed
"""

import time

from ..transcript import clean_transcript
from .models import (STATUS_DONE, STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING,
                     TranscriptError, TranscriptErrorCode, TranscriptResult,
                     TranscriptSource, choose_error)
from .providers import (ASR_MODE_LOCAL, ASR_MODE_OFF, asr_mode, asr_provider_status,
                        default_providers, find_in_chain, mode_enabled_kinds)
from .runs import TranscriptRunLog


class TranscriptService:
    """把一条 ContentItem 变成 transcript 的全部逻辑。

    store    ：FactoryStore（读写 content_items；不新增表、不加列）
    settings ：FactorySettings，可为 None（此时用 provider 默认配置）
    providers：provider 链，默认 [字幕, 本机 ASR, 外部 ASR]
    emit     ：进度回调，收到 {"id","state","message","stage",...}
    """

    def __init__(self, store, settings=None, providers=None, emit=None,
                 run_log=None, transcriber=None, asr_probe=None, http=None):
        self.store = store
        self.settings = settings
        self._emit_callback = emit if callable(emit) else None
        self.run_log = run_log if run_log is not None else TranscriptRunLog()
        self.providers = (list(providers) if providers is not None
                          else default_providers(settings, transcriber=transcriber,
                                                 probe=asr_probe, http=http))

    # ---- 进度事件 ------------------------------------------------------
    def set_emit(self, emit):
        self._emit_callback = emit if callable(emit) else None

    def _emit(self, event):
        if not self._emit_callback:
            return
        payload = {"id": event.get("id") or "", "state": event.get("state") or STATUS_RUNNING,
                   "message": event.get("message") or "", "stage": "transcript"}
        for key, value in (event.get("extra") or {}).items():
            payload[key] = value
        try:
            self._emit_callback(payload)
        except Exception:
            pass          # 进度回调不能影响转写本身（界面崩了也得把文本落库）

    # ---- 能力查询 ------------------------------------------------------
    def asr_mode(self):
        """当前设置页选择的模式：local / external / off。"""
        return asr_mode(self.settings)

    def providers_status(self):
        """这条链上每个来源当前能不能用 + 在当前模式下有没有被启用。"""
        mode = self.asr_mode()
        enabled = mode_enabled_kinds(mode)
        rows = asr_provider_status(self.providers)
        for row in rows:
            row["mode"] = mode
            row["enabled"] = row["kind"] in enabled
        return rows

    def asr_available(self):
        """本机 ASR 后端是否可用（链里没有本机 provider 或模式关掉了为 False）。"""
        provider = find_in_chain(self.providers, TranscriptSource.LOCAL_ASR)
        if provider is None or self.asr_mode() != ASR_MODE_LOCAL:
            return False
        return bool(provider.available())

    def chain(self, allow_asr=True, providers=None):
        """算出这次实际要走的 provider 链（顺序不变：字幕 → 本机 → 外部）。

        - `allow_asr=False` 只关掉「语音识别」，已有字幕照样读 ——
          关掉 ASR 不等于放弃一条本来就有字幕的内容；
        - 设置页「ASR / 转写 = 不启用」只留字幕；「= 外部服务」跳过本机后端；
        - 调用方显式传入 providers 时以调用方为准（注入 / 测试用）。
        """
        return self._resolve_chain(allow_asr, providers)[0]

    def _resolve_chain(self, allow_asr=True, providers=None):
        if providers is not None:
            chain, mode = list(providers), self.asr_mode()
        else:
            mode = self.asr_mode()
            enabled = mode_enabled_kinds(mode)
            chain = [provider for provider in self.providers
                     if getattr(provider, "kind", "") in enabled]
        if not allow_asr:
            chain = [provider for provider in chain
                     if getattr(provider, "kind", "") == TranscriptSource.SUBTITLE]
        return chain, mode

    # ---- 主入口 --------------------------------------------------------
    def transcribe(self, item_id, allow_asr=True, force=False, providers=None):
        """把一条内容转成 transcript 并写回内容库。

        force=False（默认）：库里已经有 transcript_text 就直接复用，不重跑；
        force=True         ：忽略已有文本，重新走一遍完整链路。
        """
        started = time.time()
        item_id = str(item_id or "")
        item = self.store.item(item_id) if item_id else None
        if not item:
            return self._failure(
                TranscriptResult(item_id=item_id, status=STATUS_FAILED,
                                 elapsed=self._elapsed(started)),
                TranscriptError(TranscriptErrorCode.ITEM_MISSING, "内容不存在",
                                retryable=False),
                persist=False)

        existing = str(item.get("transcript_text") or "").strip()
        if existing and not force:
            return self._cached_result(item, existing, started)

        self.store.update_item(item_id, transcript_status=STATUS_RUNNING, last_error="")

        chain, mode = self._resolve_chain(allow_asr, providers)
        errors, attempts = [], 0
        for provider in chain:
            if not provider.available():
                errors.append(provider.unavailable_error())
                continue
            attempts += 1
            self._emit({"id": item_id, "state": STATUS_RUNNING,
                        "message": provider.start_message,
                        "extra": {"source": provider.kind, "provider": provider.name}})
            try:
                output = provider.fetch(item)
            except TranscriptError as exc:
                errors.append(exc)
                if exc.code == TranscriptErrorCode.CANCELLED:
                    break
                continue
            except Exception as exc:      # provider 自己的 bug 不能炸掉整条链路
                errors.append(TranscriptError(
                    TranscriptErrorCode.UNKNOWN,
                    f"{provider.label}异常：{type(exc).__name__}: {exc}",
                    provider=provider.name))
                continue

            if output is None:
                continue                  # 这个来源没有输入（例如压根没有字幕），交给下一个
            text = clean_transcript(output.text)
            if not text:
                errors.append(TranscriptError(
                    TranscriptErrorCode.EMPTY_RESULT,
                    f"{provider.label}没有返回可用文本", provider=provider.name))
                continue
            return self._succeed(item, provider, output, text, attempts, started)

        if not allow_asr:
            errors.append(TranscriptError(
                TranscriptErrorCode.ASR_DISABLED,
                "该内容没有字幕轨，且已关闭语音识别",
                retryable=False,
                hint="需要允许语音识别，或手工粘贴字幕文本"))
        elif mode == ASR_MODE_OFF:
            errors.append(TranscriptError(
                TranscriptErrorCode.ASR_DISABLED,
                "设置里已关闭语音识别（ASR / 转写 = 不启用）",
                retryable=False,
                hint="在「设置 → AI 加工设置 → ASR / 转写」里选「OpenAI Whisper（本地）」"
                     "或「外部服务」后重试；也可以手工粘贴字幕文本"))

        error = choose_error(errors)
        return self._failure(
            TranscriptResult(item_id=item_id, status=STATUS_FAILED, attempts=attempts,
                             elapsed=self._elapsed(started)), error)

    def retry(self, item_id, allow_asr=True, force=False):
        """失败后重试：重新走一遍完整链路，成功后清掉失败原因。

        done 的内容默认不重跑（避免把已经好的文本重跑坏）；确实要重跑用 force=True。
        """
        item_id = str(item_id or "")
        item = self.store.item(item_id) if item_id else None
        if not item:
            return self._failure(
                TranscriptResult(item_id=item_id, status=STATUS_FAILED),
                TranscriptError(TranscriptErrorCode.ITEM_MISSING, "内容不存在",
                                retryable=False),
                persist=False)
        done = (item.get("transcript_status") == STATUS_DONE
                and str(item.get("transcript_text") or "").strip())
        if done and not force:
            return self.transcribe(item_id, allow_asr=allow_asr)
        result = self.transcribe(item_id, allow_asr=allow_asr, force=True)
        result.retried = True
        return result

    def retry_failed(self, ids=None, limit=20, allow_asr=True):
        """批量重试失败项（同步执行，不是队列：本模块不负责通用任务调度）。"""
        if ids:
            rows = [self.store.item(str(item_id)) for item_id in ids]
        else:
            rows = self.store.items(limit=500)
        targets = [row for row in (rows or [])
                   if row and row.get("transcript_status") == STATUS_FAILED][:int(limit or 20)]
        results = [self.retry(row["id"], allow_asr=allow_asr) for row in targets]
        return {
            "ok": all(result.ok for result in results) if results else True,
            "retried": len(results),
            "succeeded": sum(1 for result in results if result.ok),
            "failed": sum(1 for result in results if not result.ok),
            "results": [result.to_dict() for result in results],
        }

    # ---- 状态查询 ------------------------------------------------------
    def status(self, item_id):
        """当前转写状态（重启后仍然可用：文本 / 状态 / 原因都在内容库里）。"""
        item_id = str(item_id or "")
        item = self.store.item(item_id) if item_id else None
        if not item:
            return {"ok": False, "itemId": item_id, "status": "",
                    "error": TranscriptError(TranscriptErrorCode.ITEM_MISSING,
                                             "内容不存在", retryable=False).to_dict()}
        record = self.run_log.load(item_id)
        text = str(item.get("transcript_text") or "")
        status = str(item.get("transcript_status") or STATUS_PENDING)
        error = None
        if status == STATUS_FAILED:
            error = TranscriptError(
                record.get("code") or TranscriptErrorCode.UNKNOWN,
                item.get("last_error") or "转写失败",
                provider=record.get("provider") or "",
                retryable=record.get("retryable", True),
                hint=record.get("hint") or "").to_dict()
        return {
            "ok": True,
            "itemId": item_id,
            "status": status,
            "text": text,
            "chars": len(text),
            "hasText": bool(text.strip()),
            "source": record.get("source") or (
                TranscriptSource.SUBTITLE if (text and item.get("local_subtitle_path"))
                else TranscriptSource.MANUAL if text else ""),
            "provider": record.get("provider") or "",
            "subtitlePath": item.get("local_subtitle_path") or "",
            "mediaPath": item.get("local_audio_path") or item.get("local_video_path") or "",
            "error": error,
            "updatedAt": record.get("updatedAt") or item.get("updated_at") or "",
        }

    def failed_items(self, limit=20):
        """转写失败的内容（界面「重试」入口 / 诊断用）。"""
        return [row for row in self.store.items(limit=500)
                if row.get("transcript_status") == STATUS_FAILED][:int(limit or 20)]

    # ---- 内部：落库 ----------------------------------------------------
    def _cached_result(self, item, text, started):
        record = self.run_log.load(item["id"])
        return TranscriptResult(
            item_id=item["id"], ok=True, text=text,
            source=record.get("source") or TranscriptSource.CACHED,
            status=str(item.get("transcript_status") or STATUS_DONE),
            provider=record.get("provider") or "",
            asset=str(item.get("local_subtitle_path") or ""),
            chars=len(text), cached=True, elapsed=self._elapsed(started),
            notes=["库里已有转写文本，未重跑（force=True 可强制重跑）"])

    def _succeed(self, item, provider, output, text, attempts, started):
        fields = {"transcript_text": text, "transcript_status": STATUS_DONE,
                  "last_error": ""}
        field = getattr(provider, "asset_field", "")
        if output.asset and field:
            fields[field] = str(output.asset)
        self.store.update_item(item["id"], **fields)
        chars = len(text)
        self.run_log.save(item["id"], {
            "status": STATUS_DONE, "source": provider.kind, "provider": provider.name,
            "code": "", "message": "", "chars": chars, "attempts": attempts,
            "asset": str(output.asset or ""), "retryable": False})
        self._emit({"id": item["id"], "state": STATUS_RUNNING,
                    "message": provider.done_message(chars),
                    "extra": {"transcriptChars": chars, "source": provider.kind,
                              "provider": provider.name}})
        return TranscriptResult(
            item_id=item["id"], ok=True, text=text, source=provider.kind,
            status=STATUS_DONE, provider=provider.name, asset=str(output.asset or ""),
            chars=chars, attempts=attempts, elapsed=self._elapsed(started))

    def _failure(self, result, error, persist=True):
        result.ok = False
        result.error = error
        result.status = STATUS_FAILED
        result.source = ""
        if persist and result.item_id:
            self.store.update_item(result.item_id, transcript_status=STATUS_FAILED,
                                   last_error=error.message)
            self.run_log.save(result.item_id, {
                "status": STATUS_FAILED, "source": "", "provider": error.provider,
                "code": error.code, "message": error.message, "hint": error.hint,
                "retryable": error.retryable, "chars": 0, "attempts": result.attempts})
            self._emit({"id": result.item_id, "state": STATUS_FAILED,
                        "message": error.message,
                        "extra": {"error": error.to_dict(), "retryable": error.retryable}})
        return result

    @staticmethod
    def _elapsed(started):
        return round(time.time() - started, 3)
