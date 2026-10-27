# -*- coding: utf-8 -*-
"""wb_usage.py —— 本地用量 / 限流 / 活跃度聚合（纯标准库，只读）

数据来源（全部在 %USERPROFILE%\\.workbuddy 下，只读不改）：
  1) workbuddy.db
     - sessions        : 会话归属（user_id）、标题、模型、时间
     - session_usage   : 每个会话的 credit_json（每回合积分）、used/size（上下文占用）
  2) projects/<编码cwd>/<sessionId>.jsonl  底稿
     - providerData.conversationRequestId  与 credit_json 的 key 对应 → 定位回合时间
     - providerData.requestModelName       该回合用的模型
     - providerData.rawUsage               prompt_tokens / completion_tokens
     - providerData.error.status=429       限流（message 里带服务端给的重置时间）

为什么要缓存：底稿可能很大（本机最大 159MB），逐个 JSON 解析太慢。这里按
(路径, mtime, size) 做磁盘缓存，只在文件变动时重解析；解析时先用子串预筛，
避免对每一行都跑 json.loads。
"""
import json
import os
import re
import sqlite3
import threading
import time

WB_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy")
DB_PATH = os.path.join(WB_DIR, "workbuddy.db")
PROJECTS_DIR = os.path.join(WB_DIR, "projects")

# 限流重置时间：中文「将在 2026-10-11 17:51:58 UTC+8 重置」/ 英文 retry after 兜底
RESET_RE = re.compile(r"将在\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
RESET_RE_EN = re.compile(r"reset(?:\s+at)?\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", re.I)

_USAGE_CACHE_SCHEMA = 2


def _ro_uri(path):
    p = os.path.abspath(path).replace("\\", "/")
    return "file:%s?mode=ro" % p


def db_available():
    return os.path.exists(DB_PATH)


def connect():
    c = sqlite3.connect(_ro_uri(DB_PATH), uri=True, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- 基础读取

def load_accounts_meta():
    """uid -> {nick, sessions, last, first}（纯 DB，不依赖主程序）"""
    out = {}
    try:
        c = connect()
        for r in c.execute(
                "select user_id, count(*) as n, max(coalesce(last_activity_at,updated_at,created_at)) as last,"
                " min(created_at) as first from sessions where user_id is not null and user_id<>''"
                " group by user_id"):
            out[r["user_id"]] = {"uid": r["user_id"], "sessions": r["n"],
                                 "last": r["last"] or 0, "first": r["first"] or 0}
        c.close()
    except Exception:
        pass
    return out


def load_sessions():
    """会话基础信息（含 model 字段，用于限流归属兜底）"""
    out = {}
    try:
        c = connect()
        for r in c.execute(
                "select id, user_id, title, custom_title, model, created_at, updated_at,"
                " last_activity_at, deleted_at, transport from sessions"):
            out[r["id"]] = dict(r)
        c.close()
    except Exception:
        pass
    return out


def load_usage_rows():
    out = {}
    try:
        c = connect()
        for r in c.execute("select session_id, used, size, updated_at, credit_json from session_usage"):
            out[r["session_id"]] = dict(r)
        c.close()
    except Exception:
        pass
    return out


def transcript_path(session_id):
    try:
        for d in os.listdir(PROJECTS_DIR):
            p = os.path.join(PROJECTS_DIR, d, session_id + ".jsonl")
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 底稿解析

def _scan_transcript(path):
    """解析一份底稿 → (turns, limits)

    turns  : {conversationRequestId: [ts, model, prompt_tokens, completion_tokens, cache_read]}
    limits : [[ts, status, message, rid], ...]  只留 429 / 频率限制类
    """
    turns = {}
    limits = []
    # 子串预筛：不含这些关键字的行直接跳过，避免无谓的 json.loads
    # （底稿里大量 function_call / function_call_result 行没有 providerData）
    key_cr = "conversationRequestId"
    key_ru = "rawUsage"
    key_er = '"error"'
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                has_cr = key_cr in line
                has_ru = key_ru in line
                has_er = key_er in line
                if not has_cr and not has_ru and not has_er:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                pd = d.get("providerData")
                if not isinstance(pd, dict):
                    continue
                ts = d.get("timestamp") or 0
                rid = pd.get("conversationRequestId")
                model = pd.get("requestModelName") or pd.get("model") or ""
                ru = pd.get("rawUsage")
                if rid:
                    cur = turns.get(rid)
                    if cur is None:
                        turns[rid] = [ts, model, 0, 0, 0]
                        cur = turns[rid]
                    else:
                        if ts and (not cur[0] or ts < cur[0]):
                            cur[0] = ts
                        if model and not cur[1]:
                            cur[1] = model
                if isinstance(ru, dict):
                    pin = int(ru.get("prompt_tokens") or 0)
                    pout = int(ru.get("completion_tokens") or 0)
                    cread = int(ru.get("prompt_cache_hit_tokens")
                                or (ru.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                    if rid and rid in turns:
                        t = turns[rid]
                        # 以最大值为准（同回合可能有多条分片）
                        t[2] = max(t[2], pin)
                        t[3] = max(t[3], pout)
                        t[4] = max(t[4], cread)
                    else:
                        turns.setdefault("__orphan__", [ts, model, 0, 0, 0])
                        t = turns["__orphan__"]
                        t[2] += pin
                        t[3] += pout
                        t[4] += cread
                if has_er:
                    err = pd.get("error")
                    if isinstance(err, dict):
                        st = err.get("status")
                        msg = str(err.get("message") or "")
                        low = msg.lower()
                        is_limit = (str(st) == "429" or err.get("code") == 6004
                                    or "频率限制" in msg or "rate limit" in low
                                    or "too many requests" in low)
                        if is_limit:
                            limits.append([ts, st, msg, rid or ""])
    except Exception:
        pass
    return turns, limits


class Cache(object):
    """底稿解析结果的磁盘缓存：(路径, mtime, size) 不变则复用。"""

    def __init__(self, cache_path):
        self.path = cache_path
        self.data = {}
        self.lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("schema") == _USAGE_CACHE_SCHEMA:
                self.data = d.get("files") or {}
        except Exception:
            self.data = {}

    def save(self):
        try:
            d = os.path.dirname(self.path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"schema": _USAGE_CACHE_SCHEMA, "files": self.data},
                          f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
        except Exception:
            pass

    def get(self, path):
        try:
            st = os.stat(path)
        except Exception:
            return None
        ent = self.data.get(path)
        if not ent:
            return None
        if ent.get("mtime") != int(st.st_mtime) or ent.get("size") != st.st_size:
            return None
        turns = {}
        for k, v in (ent.get("turns") or {}).items():
            turns[k] = list(v)
        return turns, [list(x) for x in (ent.get("limits") or [])]

    def put(self, path, turns, limits):
        try:
            st = os.stat(path)
        except Exception:
            return
        with self.lock:
            self.data[path] = {"mtime": int(st.st_mtime), "size": st.st_size,
                               "turns": turns, "limits": limits}


# ---------------------------------------------------------------- 聚合

def _day_of(ms):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ms / 1000.0))
    except Exception:
        return ""


def _parse_reset_ts(msg, now_ms):
    """从限流消息里取服务端给的重置时间（毫秒）；取不到返回 0。"""
    for rx in (RESET_RE, RESET_RE_EN):
        m = rx.search(msg or "")
        if m:
            s = m.group(1).replace("T", " ")
            try:
                return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")) * 1000)
            except Exception:
                pass
    return 0


def collect(cache=None, progress=None, nick_of=None):
    """汇总全部统计数据。

    progress: 可选回调 progress(done, total, label)
    返回 dict，结构见下方 result。
    """
    t0 = time.time()
    sessions = load_sessions()
    usage = load_usage_rows()
    now_ms = int(time.time() * 1000)

    acc = {}
    day = {}
    model = {}
    acc_day = {}
    acc_model = {}
    limits = []
    active_days = set()

    # 需要解析的会话：有用量记录，或者（没有用量记录但也想统计活跃/限流）
    # 这里只解析有用量或有底稿的会话，避免全盘扫
    targets = []
    for sid, s in sessions.items():
        if s.get("deleted_at"):
            continue
        u = usage.get(sid)
        p = transcript_path(sid)
        if not p:
            continue
        if u or s.get("last_activity_at"):
            targets.append((sid, s, u, p))
    total = len(targets) or 1

    for i, (sid, s, u, p) in enumerate(targets):
        if progress:
            try:
                progress(i, total, s.get("title") or sid[:8])
            except Exception:
                pass
        parsed = cache.get(p) if cache else None
        if parsed is None:
            turns, lms = _scan_transcript(p)
            if cache:
                cache.put(p, turns, lms)
        else:
            turns, lms = parsed

        uid = s.get("user_id") or ""
        smodel = s.get("model") or ""
        # 限流：归属到会话当前 owner
        for ts, st, msg, rid in lms:
            m = ""
            if rid and rid in turns:
                m = turns[rid][1] or ""
            m = m or smodel or "未知模型"
            limits.append({"uid": uid, "session": sid, "title": s.get("title") or "",
                           "ts": ts, "status": st, "model": m, "msg": msg,
                           "reset_ts": _parse_reset_ts(msg, ts or now_ms)})

        # 活跃天数：按会话内有消息的日期（用回合时间近似）
        cj = u.get("credit_json") if u else None
        credits = {}
        if cj:
            try:
                credits = json.loads(cj)
            except Exception:
                credits = {}
        if credits:
            for rid, val in credits.items():
                try:
                    val = float(val)
                except Exception:
                    continue
                t = turns.get(rid)
                ts = t[0] if t else 0
                mdl = (t[1] if t else "") or smodel or "未知模型"
                d = _day_of(ts) if ts else ""
                if d:
                    active_days.add(d)
                # 账号
                a = acc.setdefault(uid, {"uid": uid, "credit": 0.0, "pin": 0, "pout": 0,
                                         "turns": 0, "cache_read": 0, "last_ts": 0,
                                         "sessions": set()})
                a["credit"] += val
                a["turns"] += 1
                if ts > a["last_ts"]:
                    a["last_ts"] = ts
                a["sessions"].add(sid)
                if t:
                    a["pin"] += t[2]
                    a["pout"] += t[3]
                    a["cache_read"] += t[4]
                # 天
                if d:
                    dd = day.setdefault(d, {"day": d, "credit": 0.0, "pin": 0, "pout": 0, "turns": 0})
                    dd["credit"] += val
                    dd["turns"] += 1
                    if t:
                        dd["pin"] += t[2]
                        dd["pout"] += t[3]
                # 模型
                mm = model.setdefault(mdl, {"model": mdl, "credit": 0.0, "pin": 0, "pout": 0, "turns": 0})
                mm["credit"] += val
                mm["turns"] += 1
                if t:
                    mm["pin"] += t[2]
                    mm["pout"] += t[3]
                # 账号×天 / 账号×模型（供筛选）
                ak = (uid, d)
                ad = acc_day.setdefault(ak, {"uid": uid, "day": d, "credit": 0.0,
                                             "pin": 0, "pout": 0, "turns": 0})
                ad["credit"] += val
                ad["turns"] += 1
                if t:
                    ad["pin"] += t[2]
                    ad["pout"] += t[3]
                bk = (uid, mdl)
                am = acc_model.setdefault(bk, {"uid": uid, "model": mdl, "credit": 0.0,
                                               "pin": 0, "pout": 0, "turns": 0})
                am["credit"] += val
                am["turns"] += 1
                if t:
                    am["pin"] += t[2]
                    am["pout"] += t[3]
        else:
            # 没有积分记录的会话，也用回合时间补活跃天数
            for rid, t in turns.items():
                if rid == "__orphan__":
                    continue
                if t[0]:
                    active_days.add(_day_of(t[0]))
                    aa = acc.setdefault(uid, {"uid": uid, "credit": 0.0, "pin": 0, "pout": 0,
                                              "turns": 0, "cache_read": 0, "last_ts": 0,
                                              "sessions": set()})
                    if t[0] > aa["last_ts"]:
                        aa["last_ts"] = t[0]
                    aa["pin"] += t[2]
                    aa["pout"] += t[3]
                    aa["cache_read"] += t[4]
                    aa["sessions"].add(sid)

    # 活跃连续天数（按本地有对话的日期）
    streak = 0
    try:
        cur = time.localtime()
        for back in range(0, 400):
            d = time.strftime("%Y-%m-%d", time.localtime(
                time.mktime((cur.tm_year, cur.tm_mon, cur.tm_mday, 12, 0, 0, 0, 0, -1))
                - back * 86400))
            if d in active_days:
                streak += 1
            else:
                if back == 0:
                    continue  # 今天还没聊不算断
                break
    except Exception:
        pass
    today = time.strftime("%Y-%m-%d")

    def _fin(rows, key):
        out = []
        for v in rows.values():
            d = dict(v)
            if isinstance(d.get("sessions"), set):
                d["sessions"] = len(d["sessions"])
            out.append(d)
        out.sort(key=lambda x: -x.get(key, 0))
        return out

    for a in acc.values():
        a["nick"] = (nick_of(a["uid"]) if nick_of else "") or ""

    # 未结束的限流（重置时间还没到）
    pending = [l for l in limits if l.get("reset_ts") and l["reset_ts"] > now_ms]
    # 每个模型留最近一条
    latest_by_model = {}
    for l in sorted(limits, key=lambda x: -(x["ts"] or 0)):
        latest_by_model.setdefault(l["model"], l)
    pend_by_model = {}
    for l in sorted(pending, key=lambda x: -(x["ts"] or 0)):
        if l["model"] not in pend_by_model or l["reset_ts"] > pend_by_model[l["model"]]["reset_ts"]:
            pend_by_model[l["model"]] = l

    return {
        "generated_at": time.time(),
        "elapsed": time.time() - t0,
        "total_credit": round(sum(a["credit"] for a in acc.values()), 2),
        "total_turns": sum(a["turns"] for a in acc.values()),
        "by_account": _fin(acc, "credit"),
        "by_day": sorted(day.values(), key=lambda x: x["day"]),
        "by_model": _fin(model, "credit"),
        "by_account_day": list(acc_day.values()),
        "by_account_model": list(acc_model.values()),
        "limits": sorted(limits, key=lambda x: -(x["ts"] or 0)),
        "limits_pending": pending,
        "limits_latest_by_model": list(latest_by_model.values()),
        "limits_pending_by_model": list(pend_by_model.values()),
        "active_days": sorted(active_days),
        "streak": streak,
        "active_today": today in active_days,
        "today": today,
        "sessions_scanned": len(targets),
    }


def fmt_credit(v):
    try:
        return ("%.2f" % float(v)).rstrip("0").rstrip(".")
    except Exception:
        return str(v)


def fmt_tokens(n):
    n = int(n or 0)
    if n >= 1000000:
        return "%.2fM" % (n / 1000000.0)
    if n >= 1000:
        return "%.1fK" % (n / 1000.0)
    return str(n)


if __name__ == "__main__":
    r = collect()
    print("扫描 %d 会话，耗时 %.2fs" % (r["sessions_scanned"], r["elapsed"]))
    print("总积分", r["total_credit"], "回合", r["total_turns"])
    for a in r["by_account"]:
        print("  %s %s credit=%s turns=%d tok=%s/%s" % (
            (a.get("nick") or "")[:8], a["uid"][:8], fmt_credit(a["credit"]),
            a["turns"], fmt_tokens(a["pin"]), fmt_tokens(a["pout"])))
    print("按天（近 10）:")
    for d in r["by_day"][-10:]:
        print("  %s %s (%d 回合)" % (d["day"], fmt_credit(d["credit"]), d["turns"]))
    print("模型 Top5:")
    for m in r["by_model"][:5]:
        print("  %-24s %s" % (m["model"], fmt_credit(m["credit"])))
    print("连续活跃 %d 天，今日活跃=%s" % (r["streak"], r["active_today"]))
    print("限流记录 %d 条，未解封 %d 条" % (len(r["limits"]), len(r["limits_pending"])))
    for l in r["limits_pending_by_model"]:
        print("  %s 重置于 %s" % (l["model"], time.strftime(
            "%m-%d %H:%M", time.localtime(l["reset_ts"] / 1000.0))))
