"""候选来源引用和类型关系；候选本身通过 children 提供下级，工作流不识别业务名称。"""

from copy import deepcopy

CATALOGS = {
    "options": {"label": "固定选项"},
    "recent_messages": {"label": "最近用户消息"},
    "meme_categories": {"label": "图库分类", "children": "memes"},
    "memes": {"label": "全部图库图片"},
}


def catalog_name(source):
    value = source.get("catalog", "") if isinstance(source, dict) else source
    return value if isinstance(value, str) else ""


def source_question(source):
    value = source.get("question", "") if isinstance(source, dict) else ""
    return value if isinstance(value, str) else ""


def meme_catalogs(memes):
    pictures = deepcopy(memes)
    groups = {}
    for picture in pictures:
        category = picture.get("category") or "未分类"
        groups.setdefault(category, []).append(picture)
    return {
        "memes": pictures,
        "meme_categories": [
            {"id": key, "description": key, "children": groups[key]}
            for key in sorted(groups)
        ],
    }


def example_catalogs():
    return {"recent_messages": [{"id": "sample_message", "description": "示例消息"}], **meme_catalogs([
        {"id": "sample_meme", "description": "微笑点头", "category": "开心"},
        {"id": "sample_sad_meme", "description": "委屈流泪", "category": "难过"},
    ])}


def candidate_type(question, by_id, *, child=False, seen=()):
    if question.id in seen:
        return ""
    parent_id = source_question(question.candidate_source)
    if parent_id:
        parent = by_id.get(parent_id)
        name = candidate_type(parent, by_id, child=True, seen=(*seen, question.id)) if parent else ""
    else:
        name = catalog_name(question.candidate_source)
    return CATALOGS.get(name, {}).get("children", "") if child else name


def migrate_candidate_sources(questions):
    """旧图只在唯一分类上游时迁移；运行时不再从触发关系推断来源。"""
    questions = deepcopy(questions)
    by_id = {q.id: q for q in questions}
    for q in questions:
        if catalog_name(q.candidate_source) != "memes" and q.role != "meme":
            continue
        parents = {t.get("question") for t in q.triggers or []
                   if t.get("question") in by_id
                   and catalog_name(by_id[t["question"]].candidate_source) == "meme_categories"}
        if len(parents) == 1:
            q.candidate_source = {"question": next(iter(parents))}
            for trigger in q.triggers or []:
                trigger.pop("ask_with_parent", None)
    return questions
