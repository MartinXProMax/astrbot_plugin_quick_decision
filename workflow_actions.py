"""公开的选项动作、参数校验和旧判断图到显式动作的转换。"""

from copy import deepcopy


WORKFLOW_EVENTS = {
    "message_received": "收到消息", "silence_elapsed": "达到冷场时长",
    "timer_elapsed": "定时触发",
    "reply_generated": "本轮正文生成完成",
}
SOURCES = {
    "options": "固定选项",
    "recent_messages": "最近用户消息",
    "meme_categories": "图库分类",
    "memes": "图库表情",
}
SCENES = ("group_passive", "private_passive", "group_proactive", "private_proactive")
ACTIONS = {
    "request_reply": {"label": "调用会话模型发言", "stages": ["pre_reply"], "parameters": []},
    "add_context": {"label": "将内容交给会话模型", "stages": ["pre_reply"], "parameters": ["from", "text", "label"]},
    "quote_message": {"label": "引用选中的消息", "stages": ["pre_reply"], "parameters": ["from"],
                      "source": "recent_messages"},
    "send_meme": {"label": "发送表情包", "stages": ["pre_reply", "post_reply"], "parameters": ["from", "when"],
                  "source": "memes", "when": ["on_match", "after_send"]},
}


def migrate_actions(questions):
    """只解释旧格式。显式事件从 v5 起直接生效。"""
    if all(q.actions is not None for q in questions):
        return questions, None
    questions = deepcopy(questions)
    gates = {t["event"]: q.id for q in questions if q.enabled and q.role == "action" for t in q.triggers}
    picture = next((q.id for q in questions if q.enabled and q.role == "meme"),
                   next((q.id for q in questions if q.role == "meme"), None))
    warnings = []
    for q in questions:
        role = q.role
        q.actions = {}
        if role == "action":
            event = q.triggers[0]["event"]
            inbound = event == "message_received"
            q.scenes = list(SCENES[:2] if inbound else SCENES[2:])
            q.default_option = "none"
            if inbound:
                q.aliases = {"join": "reply"}
                q.private_default = "" if q.ask_private else "reply"
            else:
                for token in ("当前时间", "冷场时长"):
                    if "{" + token + "}" not in q.instructions:
                        q.instructions += "\n" + token + "：{" + token + "}"
        else:
            triggers = []
            for trigger in q.triggers:
                if "event" not in trigger:
                    triggers.append(trigger)
                    continue
                event = trigger["event"]
                if event in gates:
                    triggers.append({**{k: v for k, v in trigger.items() if k != "event"},
                                     "question": gates[event], "option": "reply" if event == "message_received" else "proactive"})
                else:
                    warnings.append(f"{q.id} 的旧事件 {event} 缺少发言入口，已保留为待连接")
            # v4 可以同时填写事件和等价的显式上游，迁移后合并为同一条边。
            edges = {}
            for trigger in triggers:
                key = (trigger["question"], trigger["option"])
                if key not in edges or trigger.get("ask_with_parent"):
                    edges[key] = trigger
            q.triggers = list(edges.values())
        if role in ("target", "target_gate"):
            q.requires_candidates = "recent_messages"
            q.scenes = ["group_passive"]
            if "{当前时间}" not in q.instructions:
                q.instructions += "\n本轮判断时间：{当前时间}"
        if role == "target":
            q.candidate_source, q.default_option = "recent_messages", "latest"
        if role == "meme":
            q.candidate_source, q.default_option = "memes", "none"
        slots = ([(o.id, o.session_hint, o.tool) for o in q.options] if q.type == "choice" else
                 [(k, getattr(q, k + "_session_hint"), getattr(q, k + "_tool")) for k in ("true", "false")])
        for key, hint, tool in slots:
            if q.stage == "post_reply":
                continue
            actions = []
            if role == "action" and key in ("reply", "proactive"):
                actions.append({"tool": "request_reply"})
            if role == "target" and key == "*":
                actions.extend([{"tool": "add_context"}, {"tool": "quote_message"}])
            if role == "answer":
                actions.append({"tool": "add_context"})
                if q.id == "length" and key in ("short", "long"):
                    actions.append({"tool": "add_context", "label": "篇幅", "text": "简短" if key == "short" else "详细"})
            if hint:
                actions.append({"tool": "add_context", "text": hint})
            if tool and picture == q.id and key != "*":
                warnings.append(f"{q.id} 的 {key} 没有选中表情，已忽略该选项原有的发送绑定")
            elif tool and picture:
                actions.append({"tool": tool, "from": picture, "when": "after_send"})
            elif tool:
                warnings.append(f"{q.id} 的 {tool} 缺少可用的表情来源，已跳过该绑定")
            if actions:
                q.actions[key] = actions
        q.role = "answer"
    return questions, "；".join(dict.fromkeys(warnings)) or None


def validate_actions(q, by_id):
    keys = {o.id for o in q.options} if q.type == "choice" else {"true", "false"}
    if q.candidate_source not in SOURCES or (q.requires_candidates and q.requires_candidates not in ("recent_messages", "meme_categories", "memes")):
        return "候选来源无效"
    if q.candidate_source != "options" and (q.type != "choice" or "*" not in keys):
        return "动态候选需要选择题和 * 选项"
    if not isinstance(q.scenes, list) or any(s not in SCENES for s in q.scenes):
        return "场景无效"
    for value in (q.private_default, q.default_option):
        if value and (value not in keys or (value == "*" and q.candidate_source != "options")):
            return "默认选项无效"
    if not isinstance(q.aliases, dict) or any(not isinstance(k, str) or k in keys or not isinstance(v, str)
                                               or v not in keys for k, v in q.aliases.items()):
        return "选项别名无效"
    if not isinstance(q.actions, dict) or not q.actions.keys() <= keys:
        return "动作绑定的选项不存在"
    for key, actions in q.actions.items():
        if not isinstance(actions, list):
            return "选项动作必须为数组"
        for action in actions:
            if not isinstance(action, dict) or not isinstance(action.get("tool"), str):
                return "动作需要工具名"
            tool = ACTIONS.get(action["tool"])
            if not tool or q.stage not in tool["stages"] or action.keys() - {"tool", *tool["parameters"]}:
                return "工具名称、参数或执行阶段无效"
            if any(not isinstance(v, str) or not v.strip() for v in action.values()):
                return "动作参数需要非空文本"
            if "text" in action and "from" in action:
                return "内容来源和固定文本只能选一个"
            source = by_id.get(action.get("from", q.id))
            if not source or (q.stage == "pre_reply" and source.stage == "post_reply"):
                return "引用的答案不存在或尚未生成"
            if "source" in tool and (source.candidate_source != tool["source"] or (source is q and key != "*")):
                return "动作参数与候选类型不匹配"
            if "when" in action and action["when"] not in tool["when"]:
                return "工具执行时机无效"
    return None
