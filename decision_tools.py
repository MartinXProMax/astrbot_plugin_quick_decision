"""问题答案可在正文发出后调用的工具。"""


import asyncio


async def send_chain(context, umo, chain):
    from astrbot.api import logger

    if not chain.chain:
        return False
    try:
        return await context.send_message(umo, chain) is not False
    except Exception:  # noqa: BLE001
        logger.exception("向会话 %s 发送消息失败", umo)
        return False


def meme_chain(resolver, meme_id):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image

    chain = MessageChain()
    path = resolver.resolve_image(meme_id) if meme_id else None
    if path is not None:
        chain.chain.append(Image.fromFileSystem(str(path)))
    return chain


async def send_meme(context, umo, resolver, args):
    chain = meme_chain(resolver, args["meme_id"])
    if not chain.chain:
        return "skipped", "选中的表情文件不可用"
    if not await send_chain(context, umo, chain):
        return "failed", "平台发送失败"
    return "executed", ""


# 旧编辑器的工具目录。
TOOLS = {"send_meme": "发送本轮选中的表情"}


def skip_tools(decision, reason, *, when=None):
    for call in decision.tool_calls.values():
        if call.status == "planned" and (when is None or call.when == when):
            call.status, call.reason = "skipped", reason


async def run_tools(context, *, umo, resolver, decision, when="after_send"):
    if decision.dry_run or decision.failed or (when == "after_send" and not decision.should_speak()):
        skip_tools(decision, "试运行或缺少所需的正文", when=when)
        return False
    sent = False
    for call in list(decision.tool_calls.values()):
        if call.status != "planned" or call.when != when:
            continue
        # 在第一次 await 前领取，钩子重入时不能再次发送。
        call.status = "running"
        try:
            call.status, call.reason = await send_meme(context, umo, resolver, call.args)
        except asyncio.CancelledError:
            call.status, call.reason = "interrupted", "发送已中断，送达结果未确认"
            raise
        except Exception:  # noqa: BLE001
            from astrbot.api import logger

            logger.exception("执行决策工具 %s 失败", call.name)
            call.status, call.reason = "failed", "工具执行异常"
        sent |= call.status == "executed"
    return sent
