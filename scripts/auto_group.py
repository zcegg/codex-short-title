"""仅将启用后新建的未归组聊天放入已有项目或分组。"""
from pathlib import Path
import time

from codex_adapter import BackendError, generate_json
from oil_codex_title import (ROOT, atomic_json, load_config, read_json, read_settled_thread,
                             snapshot, state_path, thread_lock, valid_id, worker_slot)
from usage_ledger import usage_scope


def list_all(backend, method):
    items, cursors, params = [], set(), {"limit": 100}
    while True:
        page = backend.call(method, params)
        items.extend(page["data"])
        cursor = page.get("nextCursor")
        if not cursor:
            return items
        if cursor in cursors:
            raise BackendError("分组列表分页游标重复")
        cursors.add(cursor)
        params["cursor"] = cursor


def matching_project(cwd, projects):
    if not cwd or not Path(cwd).is_absolute():
        return None
    path, matches = Path(cwd).resolve(), {}
    for project in projects:
        for root in project.get("roots", []):
            raw = root.get("path")
            if not raw or not Path(raw).is_absolute():
                continue
            directory = Path(raw).resolve()
            if path == directory or directory in path.parents:
                matches[project["id"]] = max(matches.get(project["id"], 0), len(directory.parts))
    if not matches:
        return None
    deepest = max(matches.values())
    candidates = [key for key, depth in matches.items() if depth == deepest]
    return {"kind": "project", "id": candidates[0]} if len(candidates) == 1 else {"kind": "ambiguous"}


def choose_section(binary, config, context, sections):
    # 模型只能选择已确认存在的 ID；系统置顶分组不参与分类。
    choices = [{"id": s["id"], "name": s["name"]} for s in sections
               if s["name"].strip().casefold() not in ("pinned", "置顶")]
    if not choices:
        return None
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"section_id": {"type": "string", "enum": [""] + [s["id"] for s in choices]}},
              "required": ["section_id"]}
    result, _ = generate_json(binary, config, {**context, "sections": choices},
                              ROOT / "prompts/grouping.md", schema)
    if not isinstance(result, dict) or set(result) != {"section_id"}:
        raise ValueError("分组模型输出无效")
    selected = result["section_id"]
    if selected not in schema["properties"]["section_id"]["enum"]:
        raise ValueError("分组目标不在已有分组中")
    return {"kind": "section", "id": selected} if selected else None


def ineligible(backend, thread, root, config):
    if not config["enabled"] or not config.get("auto_group_since"):
        return "disabled"
    source = thread.get("source")
    if thread.get("ephemeral") or source == "exec" or (isinstance(source, dict) and "subAgent" in source):
        return "not_user_chat"
    created = thread.get("createdAt")
    if type(created) not in (int, float) or created <= config["auto_group_since"]:
        return "historical"
    if thread.get("projectId") or thread.get("section") or thread.get("sectionEnteredAt"):
        return "already_placed"
    if read_json(state_path(root, thread["id"])).get("locked"):
        return "locked"
    if backend.is_archived(thread["id"]):
        return "archived"
    return None


def process_group(backend, binary, thread_id, root, config, event_turn, *, selector=choose_section):
    thread_id = valid_id(thread_id)
    if not config.get("auto_group_since") or not config["enabled"]:
        return {"status": "disabled"}
    with thread_lock(root, thread_id) as acquired, usage_scope(root, "grouping", thread_id) as usage:
        if not acquired:
            return {"status": "busy"}
        path = root / "grouping" / (thread_id + ".json")
        if path.exists():
            return {"status": "already_checked"}
        thread, pending = read_settled_thread(backend, thread_id, event_turn)
        if pending:
            return {"status": pending}
        blocked = ineligible(backend, thread, root, config)
        if blocked:
            return {"status": blocked}
        before = snapshot(thread, config)
        if not before["has_messages"]:
            return {"status": "empty"}
        projects = list_all(backend, "project/list")
        target = matching_project(thread.get("cwd"), projects)
        if target and target["kind"] == "ambiguous":
            target = None
        elif not target:
            sections = list_all(backend, "threadSection/list")
            with worker_slot(root, config["max_parallel_workers"], config["model_timeout_seconds"]) as slot:
                if not slot:
                    return {"status": "busy"}
                target = selector(binary, config, before["context"], sections)

        fresh = load_config(root)
        after, pending = read_settled_thread(backend, thread_id, event_turn)
        if pending:
            return {"status": pending}
        blocked = ineligible(backend, after, root, fresh)
        if blocked:
            return {"status": blocked}
        if (fresh["auto_group_since"] != config["auto_group_since"]
                or snapshot(after, config)["fingerprint"] != before["fingerprint"]):
            return {"status": "stale_result"}
        if target:
            method = "project/list" if target["kind"] == "project" else "threadSection/list"
            if target["id"] not in {item["id"] for item in list_all(backend, method)}:
                return {"status": "target_removed"}
        # 先持久化本次尝试，避免写入后断线或用户移出时再次自动搬回。
        atomic_json(path, {"status": "pending" if target else "no_match", "target": target, "time": int(time.time())})
        if not target:
            usage.outcome = "no_match"
            return {"status": "no_match"}
        # shortcut: 官方移动接口无条件写入；读写瞬间的手动调整仍可能竞争，支持 CAS 后升级。
        if target["kind"] == "project":
            backend.call("thread/metadata/update", {"threadId": thread_id, "projectId": target["id"]})
        else:
            backend.call("thread/section/move", {"threadId": thread_id, "sectionId": target["id"]})
        verified = backend.read(thread_id)
        actual = verified.get("projectId") if target["kind"] == "project" else (verified.get("section") or {}).get("id")
        if actual != target["id"]:
            raise BackendError("分组写入后读回不一致；保留尝试记录，不自动重试")
        result = {"status": "grouped", "target": target, "verification": "metadata_only"}
        atomic_json(path, result)
        usage.outcome = "grouped"
        return result
