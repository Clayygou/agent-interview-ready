#!/usr/bin/env python3
"""agent-interview-ready 交付校验器（仅标准库）。

用法：
  python validate_doc.py <文档.md> [--evidence evidence.json] [--interview-log interview-log.json]
  python validate_doc.py --self-test

设计原则：脚本只拦「确定性、结构性」不合格，不判断内容好坏。
语义质量由 references/07-checklist.md 的模型勾选项负责。
退出码：0 = 无 error（warn 允许交付）；1 = 有 error，必须修复重跑。
"""
import argparse
import json
import os
import re
import random
import sys
from collections import Counter
from datetime import date, timedelta
from urllib.parse import urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 抽题器与校验器共用同一份 pick() 实现，避免两边算法漂移（v2）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from pick_questions import pick as bank_pick, pool_of as bank_pool
except ImportError:  # 题库脚本缺失时降级为不自复算，由 S18 给 warn
    bank_pick = bank_pool = None

# ---- v2.2：题量 / 评分 / 状态转移的唯一机器可读来源 ----
# references/02、08、09、10 只描述规则，不再各写一份数字；脚本载入 rules.json
# 覆盖内置默认值，缺文件时用同一组默认值降级，保证单文件分发仍可运行。
RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")


def load_rules():
    try:
        with open(RULES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


RULES = load_rules()


def _r(key, fallback):
    v = RULES.get(key)
    return v if v else fallback


_FB_QTY = {
    "业务面": {"3": [8, 10], "4": [6, 10], "5": [4, 6], "6": [1, 3], "7": [4, 6]},
    "HR 面": {"3": [3, 5], "4": [5, 7], "5": [7, 10], "6": [1, 2], "7": [4, 6]},
}
CHAPTER_SEQ = ["0", "1", "2", "3", "4", "5", "6", "7", "附录"]
CHAPTER_SEQ_INTERACTIVE = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "附录"]
TYPE_KEY = {"业务面": "业务面", "HR 面": "HR 面"}

QTY = {t: {int(c): tuple(v) for c, v in d.items()}
       for t, d in _r("question_counts", _FB_QTY).items()}
BUDGET = {
    0: (250, 350),
    1: (1200, 1800),
    2: (800, 1100),
    # v2：章 3 加 JD 题型映射表 + ≥3 道基础八股；章 4 加简历修改清单；
    # 章 8 每组加「示范作答」。预算同步放宽，总上限提到 13,000。
    3: {"业务面": (2800, 3800), "HR 面": (1100, 1800)},
    4: {"业务面": (1600, 2400), "HR 面": (1200, 1900)},
    5: {"业务面": (700, 1200), "HR 面": (1600, 2400)},
    6: {"业务面": (400, 900), "HR 面": (400, 700)},
    7: (300, 450),
    8: (1100, 2200),
}
CAP = {3: 300, 4: 250, 5: 300, 6: 450, 7: 120}
FLOOR = {3: 80, 4: 150, 5: 180, 6: 300, 7: 60}
TOTAL_RANGE = (8000, 13000)
TOTAL_MIN_EXCEPTION = 7000

SEC_RE = re.compile(r"^##\s+(\d|附录)｜")
Q_RE = re.compile(r"^\*\*Q(\d)-(\d+)｜(.+?)\*\*\s*$")
FENCE_RE = re.compile(r"^\s*```")
STATUS_RE = re.compile(r"^>\s*面经检索：(找到\s*(\d+)\s*条|未检索到有效面经)")
ILLEGAL_FILENAME = set('/\\:*?"<>|')

# 02 §5 的平台白名单。evidence 的 platform 必须是其中之一，且 URL 域名要对得上，
# 否则「换个平台名就能凑出 ≥2 个独立平台」。
PLATFORM_DOMAINS = {
    "牛客": ("nowcoder.com",),
    "知乎": ("zhihu.com",),
    "小红书": ("xiaohongshu.com", "xhslink.com"),
    "抖音": ("douyin.com",),
    "脉脉": ("maimai.cn",),
    "看准网": ("kanzhun.com",),
    "Glassdoor": ("glassdoor.com",),
    "Blind": ("teamblind.com",),
}
PLATFORM_ALIASES = {"牛客网": "牛客", "知乎网": "知乎", "脉脉网": "脉脉", "看准": "看准网"}
# 元说明豁免：只豁免「否定词紧贴在违规 token 左侧」的那一次出现，
# 不再整行 / 整个引用块豁免（否则正文里带一个「不得」就能绕检，见 A6/A7）。
META_BEFORE_TAG = re.compile(r"(未使用|不使用|不得|未出现|禁止|不构成)[^，。；]{0,12}$")
META_BEFORE_JUJU = re.compile(r"(不得|不写|禁止|未出现|不使用)\s*[「\"']?$")


MODE_RE = re.compile(r"^>\s*模式：(交互版|紧凑版)")
M_RE = re.compile(r"^\*\*M(\d+)｜对应 (Q\d-\d+)｜第 (\d+) 轮｜(L[1-4])\*\*\s*$")
SCORE_RE = re.compile(r"^\*\*评分\*\*：([0-4])\s*/\s*4｜(.+)$")
_FB_VERDICTS = {"0": "未作答", "1": "概念复述", "2": "有结论无依据",
                "3": "有依据缺代价", "4": "完整可讲"}
SCORE_VERDICT = {int(k): v for k, v in _r("score_verdicts", _FB_VERDICTS).items()}
VERDICTS = set(SCORE_VERDICT.values())
INTERVIEW_FIELDS = ("**回答**", "**评分**", "**失分点**", "**改进口径**",
                    "**考点索引**", "**示范作答**")
# 每题固定字段（03 §5）。D02：删掉这三项仍应被拦，不能只靠「参考答案」判定。
QUESTION_FIELDS = {
    3: ("**考察意图**", "**答题框架**", "**参考答案**", "**优先级**"),
    4: ("**考察意图**", "**答题框架**", "**参考答案**", "**优先级**"),
    5: ("**考察意图**", "**答题框架**", "**参考答案**", "**优先级**"),
    6: ("**考察意图**", "**答题框架**", "**参考答案**", "**优先级**"),
    7: ("**为什么问**", "**优先级**"),
}

# ---- v2：五条新硬约束的确定性锚点 ----
RESUME_RE = re.compile(r"^>\s*简历状态：\s*(.+)$")
JD_FP_RE = re.compile(r"^>\s*JD 指纹：(.+)$")
PROFILE_MAP_HEAD = re.compile(r"^###\s+JD 题型映射\s*$")
RESUME_LIST_HEAD = re.compile(r"^###\s+简历针对性修改清单\s*$")
DOMAINS = {"basics", "llm", "agent", "multimodal", "rag", "eval",
           "engineer", "research", "behavior"}
BASICS_MIN = _r("basics_min", {"业务面": 3, "HR 面": 0})
BEHAVIOR_MIN = _r("behavior_min", {"业务面": 0, "HR 面": 2})
FIRST_PICK_GROUP = _r("first_pick_group", {"业务面": "basics", "HR 面": "behavior"})
PICK_GROUPS = set(_r("pick_groups", ["basics", "role", "behavior"]))
REF_ANSWER_MIN = 100          # 章 3~6 每题「参考答案」最少有效字数
MODEL_ANSWER_MIN = 100        # 章 8 每组「示范作答」最少有效字数
PLACEHOLDER_RE = re.compile(r"(^|\s)(略|同上|见上文|见参考答案|待补|TBD|略去)(\s|[，。；]|$)")
EVIDENCE_ANCHOR = re.compile(r"证据|项目里|code/|第\s*\d+\s*题|\d+%|日志|实验|评测集|轨迹")
COST_ANCHOR = re.compile(r"代价|边界|替代|风险|失败|退化|误差")
STEP_RE = re.compile(r"(?:^|[^\d])([1-4])[\.、]\s*\S")
# 对话框固定选项（08 §3）。「口述作答」是占位项，v2 起禁止出现。
# v2.2：选项 → 内部事件映射放进 rules.json，消除 05/08 对「跳题」的两种解释。
_FB_OPTIONS = {"这题我不会": "dont_know", "需要提示": "hint",
               "直接作答": "answer", "换一道题": "skip"}
OPTION_EVENTS = _r("options", _FB_OPTIONS)
OPTION_WHITELIST = set(OPTION_EVENTS)

# 09 自适应难度：常量与题库复算。算法必须与 scripts/pick_questions.py 的 pick() 一致：
# 对「按 id 排序的池」用 random.Random(str(seed)).choice(ids)，保证跨平台可复现。
LEVEL_NUM = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}
ACTIONS = set(_r("actions", ["start", "advance", "followup", "hold", "retreat", "probe"]))
ACTION_LABEL = _r("action_labels", {"start": "题库随机首题", "advance": "升档",
                                    "followup": "追问", "hold": "保持",
                                    "retreat": "降档", "probe": "试探升档"})
PROBE_RULE = _r("probe", {"max": 2, "after_followups": 2, "score_range": [2, 3],
                          "max_step": 1, "reason_keyword": "试探"})
PROBE_MAX = int(PROBE_RULE["max"])
FOLLOWUP_RULE = _r("followup", {"consecutive_max": 2, "session_max": 4,
                                "based_on_min_chars": 8})
TRANSITIONS = {int(k): set(v) for k, v in _r("transitions", {
    "0": ["retreat", "hold"], "1": ["followup", "hold", "retreat"],
    "2": ["followup", "probe"], "3": ["followup", "probe"],
    "4": ["advance", "hold"]}).items()}
ADVANCE_RULE = _r("advance", {"requires_score": 4, "max_step": 1})
SESSION_RULE = _r("session", {
    "target_rounds": {"业务面": [5, 7], "HR 面": [4, 6]},
    "ended_reasons": {"completed": "达到目标轮数", "user_stopped": "用户提前结束",
                      "capability_unavailable": "对话框不可用",
                      "failed": "输入不可得或规则阻塞"},
    "early_stop_exempt": ["user_stopped", "capability_unavailable", "failed"]})
EARLY_EXEMPT = set(SESSION_RULE["early_stop_exempt"])
FILENAME_TOKEN = _r("filename_pattern", "_面试准备")
BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "question_bank.json")
# 本地知识库根目录可配置：环境变量 > Skill 同级目录 > 当前工作目录。
# 不写死任何用户的绝对路径；找不到时跳过标题核对并给 warn。
KB_CANDIDATES = [
    os.environ.get("AGENT_INTERVIEW_KB", ""),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "..", "ai-agent-interview-guide", "INTERVIEW.md"),
    os.path.join(os.getcwd(), "ai-agent-interview-guide", "INTERVIEW.md"),
]


# ---- v2.2 可靠性工具：输入预检 / 来源主机 / 日期 / 题库稳定引用 ----
def parse_iso(s):
    """严格 ISO 日期；非法值返回 None，绝不抛异常到 CLI。"""
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# 02 §5：检索页面可能根本没有发布日期。这类值不是「非法日期」，而是
# 明确声明的「日期未知」状态；附录写明「发布日期未标注」即可，条目不参与
# 时效窗口与高频统计。只有形似日期却解析失败的值才算 S0 输入错误。
UNKNOWN_DATE_TOKENS = {
    "-", "—", "–", "未标注", "发布日期未标注", "未注明", "未标日期",
    "未附日期", "无日期", "日期未知", "未知", "不详", "N/A", "n/a",
    "unknown", "undated",
}
UNKNOWN_DATE_MARKERS = ("未标注", "未注明", "未标日期", "未附日期",
                        "日期未知", "未知", "不详", "N/A", "n/a")


def is_unknown_date(s):
    return isinstance(s, str) and s.strip() in UNKNOWN_DATE_TOKENS


def date_state(s):
    """返回 (状态, date|None)，状态 ∈ known / unknown / invalid。"""
    if is_unknown_date(s):
        return "unknown", None
    d = parse_iso(s)
    return ("known", d) if d else ("invalid", None)


def appendix_date_ok(line, val):
    """附录日期与 evidence 对齐：未知日期只要求出现「未标注」类标记。"""
    if is_unknown_date(val):
        return any(t in line for t in UNKNOWN_DATE_MARKERS)
    return str(val) in line


def preflight_inputs(evidence, interview_log):
    """业务规则之前先校验根 / 成员类型、日期、分数范围。

    E04（2026-99-99）、E05（items 成员是字符串）、I08（score 是字符串）
    过去会抛 traceback；这里统一转成结构化 error，CLI 输出 JSON 而非栈。
    日期未知哨兵（未标注 / 未知 / N/A）是合法输入，不触发 S0。
    """
    errs = []
    if evidence is not None:
        if not isinstance(evidence, dict):
            errs.append("S0 evidence 根类型必须是对象")
        else:
            items = evidence.get("items")
            if items is not None and not isinstance(items, list):
                errs.append("S0 evidence.items 必须是数组")
            elif isinstance(items, list):
                for i, it in enumerate(items, 1):
                    if not isinstance(it, dict):
                        errs.append(f"S0 evidence.items 第 {i} 项不是对象")
                        continue
                    for fld in ("platform", "title", "date", "url"):
                        if fld in it and not isinstance(it[fld], str):
                            errs.append(f"S0 evidence.items 第 {i} 项 {fld} 必须是字符串")
                    if "date" in it and date_state(it["date"])[0] == "invalid":
                        errs.append(f"S0 evidence.items 第 {i} 项日期「{it['date']}」不是合法 YYYY-MM-DD，"
                                    "也不是允许的「日期未知」标记（如「未标注」）")
                    if "points" in it and not isinstance(it["points"], list):
                        errs.append(f"S0 evidence.items 第 {i} 项 points 必须是数组")
            if "searched_at" in evidence and date_state(evidence["searched_at"])[0] == "invalid":
                errs.append("S0 evidence.searched_at 不是合法 YYYY-MM-DD")
            for fld in ("high_frequency_points", "queries", "tools_used"):
                if fld in evidence and not isinstance(evidence[fld], list):
                    errs.append(f"S0 evidence.{fld} 必须是数组")
    if interview_log is not None:
        if not isinstance(interview_log, dict):
            errs.append("S0 interview-log 根类型必须是对象")
        else:
            turns = interview_log.get("turns")
            if turns is not None and not isinstance(turns, list):
                errs.append("S0 interview-log.turns 必须是数组")
            elif isinstance(turns, list):
                for i, trn in enumerate(turns, 1):
                    if not isinstance(trn, dict):
                        errs.append(f"S0 interview-log.turns 第 {i} 轮不是对象")
                        continue
                    sc = trn.get("score")
                    if isinstance(sc, bool) or not isinstance(sc, int):
                        errs.append(f"S0 第 {i} 轮 score 必须是 0~4 的整数")
                    elif sc not in SCORE_VERDICT:
                        errs.append(f"S0 第 {i} 轮 score={sc} 越界")
                    if "answered" in trn and not isinstance(trn["answered"], bool):
                        errs.append(f"S0 第 {i} 轮 answered 必须是布尔")
                    if "answer" in trn and not isinstance(trn["answer"], str):
                        errs.append(f"S0 第 {i} 轮 answer 必须是字符串")
                    if "decision" in trn and not isinstance(trn["decision"], dict):
                        errs.append(f"S0 第 {i} 轮 decision 必须是对象")
    return errs


def normalize_platform(name):
    s = str(name or "").strip()
    return PLATFORM_ALIASES.get(s, s)


def url_host(url):
    """返回 (主机名, 协议)；只接受 http/https，其他一律无效。"""
    try:
        p = urlsplit(str(url))
    except ValueError:
        return "", ""
    if p.scheme.lower() not in ("http", "https"):
        return "", ""
    return (p.hostname or "").lower().rstrip("."), p.scheme.lower()


def host_matches_platform(url, plat):
    """精确匹配或合法子域后缀匹配，杜绝 'domain in url' 的字符串包含绕过。"""
    host, _ = url_host(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d)
               for d in PLATFORM_DOMAINS.get(normalize_platform(plat), ()))


def canonical_url(url):
    """去 query / fragment 的规范 URL，用于同一篇文章跨平台转载去重。"""
    host, scheme = url_host(url)
    if not host:
        return str(url).strip().lower()
    return "%s://%s%s" % (scheme, host, urlsplit(str(url)).path.rstrip("/"))


def ended_reason_key(log):
    """把中文描述与英文枚举统一到 rules.json 的四种结束状态。"""
    raw = str((log or {}).get("ended_reason", "")).strip()
    phrases = SESSION_RULE["ended_reasons"]
    if raw in phrases:
        return raw
    for key, phrase in phrases.items():
        if phrase and phrase in raw:
            return key
    if "提前结束" in raw or "中断" in raw or "用户" in raw:
        return "user_stopped"
    if "不可用" in raw or "降级" in raw:
        return "capability_unavailable"
    if "失败" in raw or "阻塞" in raw:
        return "failed"
    return "unknown"


def _norm_title(s):
    return re.sub(r"[*\s\u3000]", "", str(s or "")).lower()


def _tokens(s):
    """中文 bigram + 英文词，用于判断题干是否仍指向同一考点（不做语义判断）。"""
    s = str(s or "").lower()
    out = set(re.findall(r"[a-z]{3,}", s))
    for seg in re.findall(r"[\u4e00-\u9fff]+", s):
        if len(seg) == 1:
            out.add(seg)
        else:
            out.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


def overlap_hits(a, b):
    return len(_tokens(a) & _tokens(b))


def kb_lookup():
    """知识库可配置定位；找不到返回空表，由调用方跳过标题核对。"""
    for cand in KB_CANDIDATES:
        if not cand or not os.path.isfile(cand):
            continue
        try:
            with open(cand, encoding="utf-8") as f:
                text = f.read().split("## 系统设计题", 1)[0]
        except OSError:
            continue
        kb = {}
        for ln in text.splitlines():
            m = re.match(r"\|\s*(\d+)\s*\|\s*([^|]+)\|", ln)
            if m:
                kb[int(m.group(1))] = _norm_title(m.group(2))
        if kb:
            return cand, kb
    return None, {}


def check_bank_references(bank):
    """题库 kb_source.item_ids 与 expected_titles 必须和本地知识库原文一致。

    返回 (错误列表, 是否已核对)。知识库不可用时返回 ([] , False)，
    不把「缺依赖」误判成题库错误。
    """
    path, kb = kb_lookup()
    if not kb or bank is None:
        return [], False
    errs = []
    for lv, data in bank["levels"].items():
        for item in data["items"]:
            src = item.get("kb_source")
            if not src:
                errs.append(f"S31 题库 {item['id']} 缺 kb_source 稳定引用")
                continue
            ids, titles = src.get("item_ids", []), src.get("expected_titles", [])
            if len(ids) != len(titles):
                errs.append(f"S31 题库 {item['id']} 题号与 expected_titles 数量不一致")
            for n, title in zip(ids, titles):
                actual = kb.get(n)
                if actual is None:
                    errs.append(f"S31 题库 {item['id']} 引用的第 {n} 题在知识库不存在")
                elif _norm_title(title) != actual:
                    errs.append(f"S31 题库 {item['id']} 第 {n} 题标题不符："
                                f"题库写「{title}」，知识库是「{actual}」")
    return errs, True


def load_bank():
    try:
        with open(BANK_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def reproduce_pick(bank, seed, level, group=None):
    """复算首题：与 pick_questions.pick() 共用实现。

    v2：台账顶层 pick_group="basics" 时只在八股热身池复算；老台账没有该字段
    时按整池复算，保证 v1 文档仍可校验。
    """
    if bank_pick is not None:
        return bank_pick(seed, level, bank=bank, group=group)["picked"]["id"]
    items = sorted(bank["levels"][level]["items"], key=lambda y: y["id"])
    if group is not None:
        items = [x for x in items if x.get("group") == group]
    return random.Random(str(seed)).choice([x["id"] for x in items])


def probe_check(turns, k, lv):
    """v2.1：第 k 轮（0-based）是否为合规的「试探性升档」(action=probe)。

    放宽只开这一个口子：同档连续追问 2 次、上一轮 2~3 分仍未升过档时，
    允许换考点升恰好 1 档，避免前 N 轮全卡 L1。其余低分升档仍由 S18 拦下。
    返回 (是否合规, 不合规原因)。
    """
    trn = turns[k]
    dec = trn.get("decision") or {}
    act = str(dec.get("action", ""))
    if act != "probe":
        return False, "action 不是 probe"
    if k < 3:
        return False, "前面不足 2 次同档追问"
    if lv[k - 1] not in LEVEL_NUM or lv[k] not in LEVEL_NUM:
        return False, "难度档非法"
    if LEVEL_NUM[lv[k]] - LEVEL_NUM[lv[k - 1]] != 1:
        return False, "升幅不是恰好 +1（禁止跳级 / 连续升档）"
    prev = turns[k - 1]
    prev_score = int(prev.get("score", -1))
    if prev_score not in (2, 3):
        return False, "上一轮 %d 分，不满足 2~3 分试探条件（0/1 不得升档，4 分走 advance）" % prev_score
    a, b = turns[k - 1], turns[k - 2]
    for t in (a, b):
        if str((t.get("decision") or {}).get("action", "")) != "followup":
            return False, "前面没有连续 2 次 followup"
        if str(t.get("level", "")) != lv[k - 1]:
            return False, "前面两次追问不在同一难度档"
    if str(a.get("followup_of", "")) != str(b.get("id", "")):
        return False, "连续追问链未接续上一轮"
    used = sum(1 for t in turns[:k]
               if str((t.get("decision") or {}).get("action", "")) == "probe")
    if used >= PROBE_MAX:
        return False, "整场试探性升档已达 %d 次上限" % PROBE_MAX
    if "试探" not in str(dec.get("reason", "")):
        return False, "reason 未写明「试探」及触发依据"
    based = str(dec.get("based_on") or "")
    if len(based) < 8 or based not in str(prev.get("answer", "")):
        return False, "based_on 不是上一轮回答原文片段（≥8 字）"
    if based not in str(trn.get("question", "")):
        return False, "题干未引用上一轮回答原句"
    if trn.get("followup_of"):
        return False, "probe 是换考点升档，不得标 followup_of"
    if str(trn.get("bank_id", "")) == str(prev.get("bank_id", "")):
        return False, "probe 必须换考点（bank_id 不得沿用被追问轮）"
    return True, ""


def budget_of(ch, t):
    v = BUDGET[ch]
    return v[t] if isinstance(v, dict) else v


def effective_chars(lines):
    """近似有效字数：中文按字、英文数字按词；去掉 Markdown 标记、代码块与附录来源之外的装饰。"""
    kept, in_code = [], False
    for ln in lines:
        if FENCE_RE.match(ln):
            in_code = not in_code
            continue
        if in_code:
            continue
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", ln)
        s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
        s = re.sub(r"[#>*`_|]", " ", s)
        s = re.sub(r"^\s*\d+[\.\)]\s*", " ", s)
        s = re.sub(r"🔴|🔵|【[^】]*】", " ", s)
        kept.append(s)
    text = "\n".join(kept)
    return len(re.findall(r"[\u4e00-\u9fff]", text)) + len(re.findall(r"[A-Za-z0-9]+", text))


def parse_sections(lines):
    """跳过代码块，避免规则文件里的格式示例被误判成真实章节。"""
    out, in_code = [], False
    for i, ln in enumerate(lines):
        if FENCE_RE.match(ln):
            in_code = not in_code
            continue
        if in_code:
            continue
        if (m := SEC_RE.match(ln)):
            out.append((m.group(1), i))
    return out


def chapter_of(idx, secs):
    cur = None
    for tok, start in secs:
        if start <= idx:
            cur = tok
        else:
            break
    return cur


def validate(text, evidence=None, filename="交付文档.md", interview_log=None):
    lines = text.splitlines()
    errors, warnings, info = [], [], {}
    E = lambda m: errors.append(m)
    W = lambda m: warnings.append(m)

    # S0：类型 / 日期 / 分数范围预检。异常输入返回结构化 JSON，不再抛 traceback。
    pf = preflight_inputs(evidence, interview_log)
    if pf:
        return {"file": os.path.basename(filename or ""), "interview_type": None,
                "info": {"preflight": "failed"}, "errors": pf,
                "warnings": [], "passed": False}

    mode_m = next((m for ln in lines[:15] if (m := MODE_RE.match(ln.strip()))), None)
    mode = mode_m.group(1) if mode_m else "紧凑版"
    info["mode"] = mode

    # S12 文件名
    base = os.path.basename(filename or "")
    if not base.endswith(".md") or any(c in base for c in ILLEGAL_FILENAME):
        E("S12 文件名非法：须为 .md 且不含 / \\ : * ? \" < > |")
    elif FILENAME_TOKEN not in base:
        E(f"S12 文件名须含「{FILENAME_TOKEN}」（公司_岗位_面试准备.md，允许 _probe样例 等后缀）")

    # S1 章节齐备与顺序
    secs = parse_sections(lines)
    tokens = [t for t, _ in secs]
    expected = CHAPTER_SEQ_INTERACTIVE if mode == "交互版" else CHAPTER_SEQ
    if tokens != expected:
        E(f"S1 章节顺序/齐备不合格：期望 {expected}，实际 {tokens}")
    appendix_idx = next((i for t, i in secs if t == "附录"), len(lines))
    bounds = {}
    for pos, (tok, start) in enumerate(secs):
        end = secs[pos + 1][1] if pos + 1 < len(secs) else len(lines)
        bounds[tok] = (start, end)

    # S2 头部元信息
    head = "\n".join(lines[:15])
    # 只认「面试类型：」后面的档位，避免把「业务面（非 HR 面）」误判成 HR 面。
    mt = re.search(r"面试类型：\s*(业务面|HR\s*面)", head)
    t = "HR 面" if mt and "HR" in mt.group(1) else "业务面"
    info["interview_type"] = t
    if "面试类型：" not in head:
        E("S2 头部缺少「面试类型：」")
    else:
        raw_type = re.search(r"面试类型：\s*([^｜|\n]+)", head)
        if raw_type and not re.fullmatch(r"(业务面|HR\s*面)(\s*（[^）]*）)?",
                                         raw_type.group(1).strip()):
            E(f"S2 面试类型「{raw_type.group(1).strip()}」不在枚举 业务面 / HR 面")
    if not re.search(r"生成日期：\d{4}-\d{2}-\d{2}", head):
        E("S2 头部缺少 YYYY-MM-DD 生成日期")
    # S24：简历状态三态，决定章 4 的「简历针对性修改清单」是强要求还是边界说明。
    resume_m = next((m for ln in lines[:20] if (m := RESUME_RE.match(ln.strip()))), None)
    rs = str(resume_m.group(1)).strip() if resume_m else ""
    if not rs:
        E("S24 头部缺少固定的「> 简历状态：…」行（已解析（真实简历）/ 占位 / 未提供）")
        resume_state = None
    elif "占位" in rs or "模板" in rs:
        resume_state = "placeholder"
    elif "未提供" in rs or "无简历" in rs:
        resume_state = "none"
    elif "已解析" in rs:
        resume_state = "real"
    else:
        E(f"S24 简历状态「{rs}」无法归一化为 已解析 / 占位 / 未提供")
        resume_state = None
    info["resume_status"] = resume_state
    status = None
    for ln in lines[:20]:
        if (m := STATUS_RE.match(ln.strip())):
            status = ("found", int(m.group(2))) if m.group(2) else ("none", 0)
            break
    if status is None:
        E("S2 缺少固定的「> 面经检索：…」状态行")

    # S17 模式行与章 0 声明是否自相矛盾
    if mode_m is None:
        W("S17 头部缺少「> 模式：交互版 / 紧凑版」，按紧凑版处理")
    if mode == "交互版" and "未进行模拟面试" in "\n".join(lines[:30]):
        E("S17 交互版章 0 不得声明「未进行模拟面试」")

    # S11 排版硬伤（代码块内的示例文本不算正文排版，跳过）
    in_code = False
    for i, ln in enumerate(lines, 1):
        if FENCE_RE.match(ln):
            in_code = not in_code
            continue
        if in_code:
            continue
        if re.match(r"^#{4,}\s", ln):
            E(f"S11 第 {i} 行出现四级标题（手机端标题最多三级）")
        if ln.strip().startswith("|"):
            cols = ln.strip().strip("|").count("|") + 1
            if cols > 4:
                E(f"S11 第 {i} 行表格 {cols} 列，超过 4 列上限")
        if re.search(r"<style|style=|color:|<font|<img|<div", ln, re.I):
            E(f"S11 第 {i} 行含 CSS / HTML 样式或外部图片")
        if "🔴" in ln and "必背" not in ln:
            E(f"S11 第 {i} 行 🔴 未带文字标签「必背」")
        if "🔵" in ln and "加分" not in ln:
            E(f"S11 第 {i} 行 🔵 未带文字标签「加分」")

    # S3 总有效字数（不含附录来源）
    total = effective_chars(lines[:appendix_idx])
    info["effective_chars"] = total
    low, high = TOTAL_RANGE
    if total > high:
        E(f"S3 有效字数 {total} 超过硬上限 {high}")
    elif total < TOTAL_MIN_EXCEPTION:
        E(f"S3 有效字数 {total} 低于最低例外 {TOTAL_MIN_EXCEPTION}")
    elif total < low:
        W(f"S3 有效字数 {total} 低于 {low}：须在章 0 标注信息边界")

    # S4/S5 题目解析
    qs = []
    for i, ln in enumerate(lines):
        if (m := Q_RE.match(ln.strip())):
            qs.append({"ch": int(m.group(1)), "num": int(m.group(2)),
                       "title": m.group(3), "line": i})
    stops = sorted(set([q["line"] for q in qs] + [i for _, i in secs] + [len(lines)]))
    design_titles = {id(q) for q in qs
                     if q["ch"] == 3 and re.search(r"设计|架构|方案", q["title"])}
    if len(design_titles) > 2:
        E(f"S5 章 3 方案/系统设计题 {len(design_titles)} 道，超过 2 道上限")

    qcount = Counter(q["ch"] for q in qs)
    info["question_counts"] = {str(k): v for k, v in sorted(qcount.items())}
    for ch, (lo, hi) in QTY[t].items():
        if not lo <= qcount.get(ch, 0) <= hi:
            E(f"S4 章 {ch} 题量 {qcount.get(ch, 0)} 不在 {lo}~{hi}（{t}）")
    for ch in (3, 4, 5, 6, 7):
        nums = sorted(q["num"] for q in qs if q["ch"] == ch)
        if nums and nums != list(range(1, len(nums) + 1)):
            E(f"S4 章 {ch} 的 Q 编号不连续：{nums}")
    for title, n in Counter(q["title"] for q in qs).items():
        if n > 1:
            E(f"S4 题目「{title}」出现 {n} 次，违反同一考点最多 1 次")

    for q in qs:
        if chapter_of(q["line"], secs) != str(q["ch"]):
            E(f"S4 Q{q['ch']}-{q['num']} 的章节号与其所在章不一致")
            continue
        end = min(x for x in stops if x > q["line"])
        # 章末的「备用口径」引用块、分节线与下一章标题属于章级内容，不属于
        # 任何单题；计入章节最后一题会造成单题字数假阳性。
        block = [ln for ln in lines[q["line"]:end]
                 if not ln.strip().startswith(">") and ln.strip() != "---"
                 and not ln.startswith("## ")]
        n = effective_chars(block)
        cap = 450 if id(q) in design_titles else CAP.get(q["ch"], 300)
        if n > cap:
            E(f"S5 Q{q['ch']}-{q['num']} 单题 {n} 字，超过上限 {cap}")
        elif n < FLOOR.get(q["ch"], 0):
            W(f"S5 Q{q['ch']}-{q['num']} 单题 {n} 字，偏短（建议 ≥{FLOOR.get(q['ch'])}）")

    # S6 各章预算（缺章为空判 error，偏差只 warn）
    for ch in range(0, 9):
        if str(ch) not in bounds:
            continue
        s, e = bounds[str(ch)]
        n = effective_chars(lines[s:e])
        blo, bhi = budget_of(ch, t)
        if n < 30:
            E(f"S6 章 {ch} 内容为空")
        elif n > bhi * 2:
            E(f"S6 章 {ch} 有效字数 {n}，超过预算上限 {bhi} 的 2 倍（注水）")
        elif n < blo * 0.5 or n > bhi * 1.5:
            W(f"S6 章 {ch} 有效字数 {n}，明显偏离预算 {blo}~{bhi}")

    # S7 引用编号与附录
    body = "\n".join(lines[:appendix_idx])
    cites = sorted({int(x) for x in re.findall(r"\[(\d+)\]", body)})
    appendix = lines[appendix_idx:]
    entries = [int(m.group(1)) for ln in appendix
               if (m := re.match(r"^(\d+)\.\s", ln.strip()))]
    info["citations"] = cites
    info["source_entries"] = len(entries)
    if not re.search(r"^###\s+公司资料", "\n".join(appendix), re.M):
        E("S7 附录缺少「### 公司资料」组")
    if not re.search(r"^###\s+面经", "\n".join(appendix), re.M):
        E("S7 附录缺少「### 面经」组")
    if len(entries) > 20:
        E(f"S7 来源 {len(entries)} 条，超过 20 条上限")
    if entries and entries != list(range(1, len(entries) + 1)):
        E(f"S7 来源编号不连续：{entries}")
    missing = [c for c in cites if c not in entries]
    if missing:
        E(f"S7 正文引用编号 {missing} 在附录中没有对应条目")
    # 反向可回溯：附录里从未被正文引用的孤立来源。合法的备用来源 / 章首统一引用
    # 会命中，故只给 warn 不阻断。
    orphan = [n for n in entries if n not in cites]
    if orphan:
        W(f"S7 附录来源 {orphan} 未被正文 [编号] 引用；若属备用来源请在交付说明中标注")
    # 06 §2：来源条目要有名称、平台/机构、时间、链接（无链接须写明「未附链接」）。
    # 只验编号连续与 ≤20 条会让重复占位条目也能交付，故逐条检查字段。
    for ln in appendix:
        if not re.match(r"^\d+\.\s", ln.strip()):
            continue
        has_link = "](" in ln or "未附链接" in ln
        has_date = (bool(re.search(r"\d{4}-\d{2}-\d{2}", ln))
                    or "未标注" in ln or "未附链接" in ln)
        if not (has_link and has_date):
            E(f"S7 附录来源缺少链接或日期：{ln.strip()[:40]}")

    # 面经组条目数
    mian_start = next((i for i, ln in enumerate(lines)
                       if re.match(r"^###\s+面经", ln)), None)
    mian_count = 0
    if mian_start is not None:
        for ln in lines[mian_start + 1:]:
            if re.match(r"^#{2,3}\s", ln):
                break
            if re.match(r"^\d+\.\s", ln.strip()):
                mian_count += 1

    # S8~S10 面经状态与证据一致性
    # 逐次出现统计；只豁免两类「元说明」：否定词紧贴左侧，或 token 被反引号 /
    # 直角引号包裹（术语提及）。旧版整行 / 整个引用块豁免可被引用块和一个
    # 「不得」绕过，故只豁免紧贴的否定词与术语引用。
    def count_token(tok, exempt_re):
        n = 0
        for ln in lines:
            pos = 0
            while True:
                i = ln.find(tok, pos)
                if i < 0:
                    break
                left = ln[max(0, i - 24):i]
                before = ln[i - 1] if i else ""
                after = ln[i + len(tok)] if i + len(tok) < len(ln) else ""
                quoted = (before == after and before == "`") or (before == "「" and after == "」")
                if not quoted and not exempt_re.search(left):
                    n += 1
                pos = i + len(tok)
        return n

    hf_tags = count_token("【面经高频】", META_BEFORE_TAG)
    juju_hits = count_token("据面经", META_BEFORE_JUJU)
    has_evidence = bool(evidence and evidence.get("found")
                        and evidence.get("tools_used") and evidence.get("items"))
    info["evidence_found"] = has_evidence
    info["hf_tags"] = hf_tags
    if has_evidence:
        items = evidence["items"]
        n = len(items)
        if status is None or status[0] != "found" or status[1] != n:
            E(f"S9 章 0 面经状态与 evidence 不一致：应有「找到 {n} 条」")
        if mian_count != n:
            E(f"S9 附录面经组 {mian_count} 条，与 evidence 的 {n} 条不一致")
        searched = parse_iso(evidence.get("searched_at")) or date.today()
        cutoff = searched - timedelta(days=548)
        # S9a 平台归一化 + 状态行日期窗口（E06 / E09：别名、缺窗口都不再放过）
        status_line = next((ln for ln in lines[:20] if STATUS_RE.match(ln.strip())), "")
        win = re.search(r"（([^）]*)；\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})）",
                        status_line)
        if not win:
            E("S9 状态行缺少「（平台集合；起始日期 ~ 结束日期）」窗口")
        else:
            declared = {normalize_platform(p) for p in re.split(r"[/、,，]", win.group(1))
                        if p.strip()}
            actual = {normalize_platform(it.get("platform", "")) for it in items}
            if declared != actual:
                E(f"S9 状态行平台 {sorted(declared)} 与 evidence 的 {sorted(actual)} 不一致")
            lo, hi = parse_iso(win.group(2)), parse_iso(win.group(3))
            if not lo or not hi or lo > hi:
                E("S9 状态行日期窗口非法")
            else:
                if hi > searched:
                    E("S9 状态行窗口结束晚于检索日期")
                for it in items:
                    d = parse_iso(it.get("date", ""))
                    if d and not (lo <= d <= hi):
                        E(f"S9 面经日期 {d} 不在状态行窗口 {lo}~{hi} 内")
        # 同一篇原文 / 同一 URL 只算一个来源；平台别名不得制造独立来源。
        for u, c in Counter(canonical_url(it.get("url", "")) for it in items).items():
            if c > 1:
                E(f"S10 同一 URL 被记为 {c} 个独立来源：{u}（平台别名不产生新来源）")
        # S9b 附录逐条与 evidence 对齐：URL / 标题 / 日期 / 平台（E08）
        mian_lines = []
        if mian_start is not None:
            for ln in lines[mian_start + 1:]:
                if re.match(r"^#{2,3}\s", ln):
                    break
                if re.match(r"^\d+\.\s", ln.strip()):
                    mian_lines.append(ln)
        for k, it in enumerate(items[:len(mian_lines)]):
            line = mian_lines[k]
            for fld, val in (("url", it.get("url", "")), ("title", it.get("title", "")),
                             ("date", it.get("date", "")),
                             ("platform", normalize_platform(it.get("platform", "")))):
                if fld == "date":
                    ok = appendix_date_ok(line, val)
                else:
                    ok = (not str(val)) or str(val) in line
                if not ok:
                    E(f"S9 附录面经第 {k + 1} 条 {fld} 与 evidence 不一致")
        # S10a 平台白名单 / URL 主机名 / 时效（E01/E02/E03）
        for it in items:
            raw_plat = str(it.get("platform", "")).strip()
            plat = normalize_platform(raw_plat)
            url = str(it.get("url", ""))
            if plat not in PLATFORM_DOMAINS:
                E(f"S10 evidence 平台「{plat}」不在 02 §5 白名单内")
            elif not host_matches_platform(url, plat):
                E(f"S10 evidence 平台「{plat}」与 URL 主机不符：{url[:60]}")
            d_state, d = date_state(it.get("date", ""))
            title20 = str(it.get("title", ""))[:20]
            if d_state == "unknown":
                # 02 §5 允许页面未标注日期；附录须如实注明，且该条不进入时效窗口。
                W(f"S10 面经「{title20}」日期未标注，不参与 18 个月时效与日期窗口校验")
            elif d_state == "invalid" or d is None:
                E(f"S10 面经「{title20}」日期缺失或非法")
            elif d > searched:
                E(f"S10 面经日期 {d} 晚于检索日期 {searched}（未来面经不参与高频统计）")
            elif d < cutoff:
                E(f"S10 面经「{title20}」日期 {d} 早于近 18 个月窗口")
        hf_points = evidence.get("high_frequency_points", [])
        if hf_tags > 5:
            E(f"S8 【面经高频】标签 {hf_tags} 处，超过 5 处上限")
        if not hf_points and hf_tags:
            E("S8 evidence 没有高频考点，文档却出现【面经高频】标签")
        # S8 标签归属：point_id → question_ids 必须由 evidence 直接给出（E07）
        labeled = [f"Q{q['ch']}-{q['num']}" for q in qs if "【面经高频】" in q["title"]]
        links = evidence.get("point_links")
        if links is None:
            if labeled:
                W("S8 evidence 缺 point_links，无法核对【面经高频】标签与考点归属")
        elif not isinstance(links, dict):
            E("S8 evidence.point_links 必须是 {考点: [Q 编号]} 对象")
        else:
            allowed = set()
            for p, qids in links.items():
                if p in hf_points and isinstance(qids, list):
                    allowed.update(str(x) for x in qids)
            stray = [x for x in labeled if x not in allowed]
            if stray:
                E(f"S8 【面经高频】标签 {stray} 没有对应的高频考点关联（point_links）")
        for p in hf_points:
            seen, plats = set(), set()
            for it in items:
                if p in it.get("points", []):
                    key = (normalize_platform(it.get("platform", "")),
                           canonical_url(it.get("url", "")))
                    if key not in seen:
                        seen.add(key)
                        plats.add(key[0])
            if len(plats) < 2:
                E(f"S10 高频考点「{p}」只出现在 {len(plats)} 个独立平台，未达 ≥2")
    else:
        if status and status[0] != "none":
            E("S9 无检索证据却写「找到 N 条」：须改为「未检索到有效面经」")
        if hf_tags:
            E("S8 无检索证据时不得出现【面经高频】标签")
        if juju_hits:
            E("S8 无检索证据时不得出现「据面经」式表述")
        if mian_count:
            W(f"S9 无检索证据，但附录面经组有 {mian_count} 条")
    if juju_hits and has_evidence:
        E("S8 全文出现「据面经」式虚构表述")

    # S13~S16 交互版：章 8 结构、评分合法性、与 interview-log.json 交叉一致
    ms = []
    doc_scores = {}
    doc_levels = {}
    if mode == "交互版":
        if "8" not in bounds:
            E("S13 交互版缺少章 8｜模拟面试记录与评判")
        else:
            s8, e8 = bounds["8"]
            body8 = lines[s8 + 1:e8]
            for _i, _ln in enumerate(body8):
                _m = M_RE.match(_ln.strip())
                if _m:
                    ms.append((_m.group(1), _m.group(2), _i))
                    doc_levels[_m.group(1)] = _m.group(4)
            if not ms:
                E("S13 章 8 没有任何 M 轮次记录")
            nums = [int(x[0]) for x in ms]
            if nums != list(range(1, len(nums) + 1)):
                E(f"S14 M 编号不连续：{nums}")
            qids = {f"Q{q['ch']}-{q['num']}" for q in qs}
            for mid, qref, _ in ms:
                if qref not in qids:
                    E(f"S14 M{mid} 的 {qref} 在正文中不存在")
            for k, (mid, qref, pos) in enumerate(ms):
                end = ms[k + 1][2] if k + 1 < len(ms) else len(body8)
                grp = [ln.strip() for ln in body8[pos:end]]
                for fld in INTERVIEW_FIELDS:
                    if not any(ln.startswith(fld) for ln in grp):
                        E(f"S13 M{mid} 缺少「{fld}」")
                score_line = next((ln for ln in grp if ln.startswith("**评分**")), "")
                sm = SCORE_RE.match(score_line)
                if not sm:
                    E(f"S15 M{mid} 评分格式非法：{score_line[:40]}")
                    continue
                score, verdict = int(sm.group(1)), sm.group(2).strip()
                doc_scores[mid] = score
                if verdict not in VERDICTS:
                    E(f"S15 M{mid} 档位「{verdict}」不在 08 §5 rubric 内")
                ans = next((ln for ln in grp if ln.startswith("**回答**")), "")
                if "未作答" in ans and (score != 0 or verdict != "未作答"):
                    E(f"S15 M{mid} 未作答却判 {score} 分 / {verdict}")
            table_rows = [ln for ln in body8 if re.match(r"^\|\s*M\d+\s*\|", ln)]
            if len(table_rows) != len(ms):
                E(f"S14 总评表 {len(table_rows)} 行，与 {len(ms)} 个 M 轮次不一致")
            review = "\n".join(ln for ln in body8 if "下轮优先复习" in ln)
            if len(re.findall(r"第\s*\d+\s*题", review)) < 3:
                E("S14 缺少含 3 条真实题号的「下轮优先复习」")
            if interview_log is None:
                E("S16 交互版必须提供 --interview-log 做交叉校验")
            else:
                turns = interview_log.get("turns", [])
                if len(turns) != len(ms):
                    E(f"S16 log 有 {len(turns)} 轮，文档有 {len(ms)} 轮")
                for k, turn in enumerate(turns[:len(ms)]):
                    mid, qref, pos = ms[k]
                    end = ms[k + 1][2] if k + 1 < len(ms) else len(body8)
                    grp_lines = [ln.strip() for ln in body8[pos:end]]

                    def fld(name):
                        for ln in grp_lines:
                            if ln.startswith(name):
                                return ln[len(name):].lstrip("：: ").strip()
                        return None

                    if str(turn.get("id", "")) != f"M{mid}":
                        E(f"S16 log 第 {k + 1} 轮 id 与 M{mid} 不一致")
                    if str(turn.get("q_ref", "")) != qref:
                        E(f"S16 log 第 {k + 1} 轮 q_ref 与 {qref} 不一致")
                    # S16 回答保真：全文逐字比对；节选必须另存 answer_excerpt + 范围。
                    ans = str(turn.get("answer", ""))
                    answered = bool(turn.get("answered", bool(ans)))
                    doc_ans = fld("**回答**")
                    if answered:
                        excerpt = turn.get("answer_excerpt")
                        want = ans if excerpt is None else str(excerpt)
                        if excerpt is not None:
                            rng = turn.get("answer_excerpt_range")
                            if not (isinstance(rng, list) and len(rng) == 2
                                    and all(isinstance(x, int) for x in rng)):
                                E(f"S16 第 {k + 1} 轮 answer_excerpt 须带 [start,end] 范围")
                            elif ans[rng[0]:rng[1]] != want:
                                E(f"S16 第 {k + 1} 轮 answer_excerpt 不是原文连续片段")
                        if doc_ans != want:
                            E(f"S16 第 {k + 1} 轮回答原文与文档不一致（须全文一致，或声明合规节选）")
                    elif doc_ans != "未作答":
                        E(f"S16 第 {k + 1} 轮为未作答，文档却写了回答文本")
                    # S15/S16 评分与档位：固定映射 + 正文 / 台账双向一致。
                    score_line = next((ln for ln in grp_lines if ln.startswith("**评分**")), "")
                    sm = SCORE_RE.match(score_line)
                    if sm:
                        sc, doc_verdict = int(sm.group(1)), sm.group(2).strip()
                        want_verdict = SCORE_VERDICT.get(sc)
                        if doc_verdict != want_verdict:
                            E(f"S15 M{mid} {sc} 分却写「{doc_verdict}」，固定映射应为「{want_verdict}」")
                        if str(turn.get("verdict", "")) != doc_verdict:
                            E(f"S16 第 {k + 1} 轮台账 verdict 与文档不一致")
                        if str(turn.get("verdict", "")) != want_verdict:
                            E(f"S16 第 {k + 1} 轮台账 score/verdict 不符固定映射")
                    if int(turn.get("score", -1)) != doc_scores.get(mid, -2):
                        E(f"S16 log 第 {k + 1} 轮评分与文档不一致")
                    if not answered and doc_scores.get(mid, -2) != 0:
                        E(f"S16 log 第 {k + 1} 轮为未作答，文档却给了分")
                    # S29 评判与本轮回答 / 台账 gaps 的引用绑定：只验「有没有引用」，
                    # 不评判语义是否中肯（那是 07 的模型勾选项）。
                    review_text = (fld("**失分点**") or "") + (fld("**改进口径**") or "")
                    gaps = [str(x) for x in (turn.get("gaps") or []) if len(str(x)) >= 4]
                    linked = any(g in review_text for g in gaps)
                    quoted = (answered and len(ans) >= 8
                              and any(ans[i:i + 8] in review_text
                                      for i in range(0, len(ans) - 7)))
                    if not (linked or quoted):
                        E(f"S29 M{mid} 评判未引用台账 gaps 或该轮回答原文，评判与回答脱钩")

                # ---- S18~S21 自适应难度轨迹（references/09）----
                bank = load_bank()
                lv = [str(t.get("level", "")) for t in turns]
                if any(x not in LEVEL_NUM for x in lv):
                    E("S18 台账有轮次缺少合法 level（L1~L4）")
                if lv and lv[0] != "L1":
                    E("S18 首轮必须 L1（从题库随机起手）")
                seed = interview_log.get("pick_seed")
                pick_group = interview_log.get("pick_group")
                if not seed:
                    E("S18 缺少 pick_seed，首题随机不可复现")
                want_group = FIRST_PICK_GROUP[t]
                if pick_group is None:
                    W("S18 台账未声明 pick_group，按 v1 全池复算；新场次须声明题组")
                elif pick_group not in PICK_GROUPS:
                    E(f"S18 pick_group「{pick_group}」非法，只能是 {sorted(PICK_GROUPS)}")
                elif pick_group != want_group:
                    msg = (f"S26 {t} 首题题组应为 {want_group}，实际 {pick_group}"
                           f"（04：HR 面不出技术细节，09：业务面从八股热身）")
                    W(msg) if pick_group == "role" else E(msg)
                if bank is None:
                    W("S18 题库文件缺失，本场未复算首题随机结果")
                elif turns and seed:
                    grp = pick_group if pick_group in PICK_GROUPS else None
                    want = reproduce_pick(bank, seed, "L1", grp)
                    got = str(turns[0].get("bank_id", ""))
                    if got != want:
                        E(f"S18 首题 bank_id「{got}」与种子 {seed}（group={grp}）复算结果「{want}」不一致")
                # S26：首题必须属于声明的题组池（业务面 basics / HR 面 behavior）。
                if pick_group in PICK_GROUPS and turns and bank is not None:
                    group_ids = {x["id"] for x in bank["levels"]["L1"]["items"]
                                 if x.get("group") == pick_group}
                    if str(turns[0].get("bank_id", "")) not in group_ids:
                        E(f"S26 首题 bank_id 不属于 L1 {pick_group} 池")
                # S33：题目 / 考点 / 编号与题库显式绑定（I03 / I09）。
                bank_items = {}
                if bank is not None:
                    for _lv, _d in bank["levels"].items():
                        for _x in _d["items"]:
                            bank_items[_x["id"]] = _x
                basics_doc_qids = {f"Q{q['ch']}-{q['num']}" for q in qs
                                   if "【基础八股】" in q["title"]}
                for k, trn in enumerate(turns):
                    bid = str(trn.get("bank_id", ""))
                    if bid and bank is not None and lv[k] in LEVEL_NUM:
                        pool = {x["id"] for x in bank["levels"][lv[k]]["items"]}
                        if bid not in pool:
                            E(f"S18 第 {k + 1} 轮 bank_id「{bid}」不属于 {lv[k]} 题库")
                    item = bank_items.get(bid)
                    if not item:
                        continue
                    act = str((trn.get("decision") or {}).get("action", ""))
                    if act != "followup":
                        if str(trn.get("topic", "")).strip() != str(item.get("topic", "")).strip():
                            E(f"S33 第 {k + 1} 轮 topic「{trn.get('topic', '')}」与题库 {bid} 不一致")
                        if overlap_hits(str(trn.get("question", "")),
                                        str(item.get("topic", "")) + " " +
                                        str(item.get("prompt", ""))) < 2:
                            E(f"S33 第 {k + 1} 轮题干与题库 {bid} 考点不对应")
                    if item.get("group") == "basics" and str(trn.get("q_ref", "")) not in basics_doc_qids:
                        E(f"S33 第 {k + 1} 轮是基础八股题，q_ref 须指向章 3「【基础八股】」题目")
                if all(x in LEVEL_NUM for x in lv):
                    for k in range(1, len(lv)):
                        d = LEVEL_NUM[lv[k]] - LEVEL_NUM[lv[k - 1]]
                        prev_score = int(turns[k - 1].get("score", -1))
                        act = str((turns[k].get("decision") or {}).get("action", ""))
                        if d > 1:
                            E(f"S18 第 {k + 1} 轮从 {lv[k - 1]} 跳到 {lv[k]}，禁止跳级")
                        # v2.1：低分升档只有显式 action=probe 一条路，合法性交 S28 复核；
                        # 用 advance / hold 等动作伪装低分升档仍然拦下。
                        if d > 0 and prev_score < 3 and act != "probe":
                            E(f"S18 第 {k} 轮仅 {prev_score} 分，第 {k + 1} 轮却升到 {lv[k]}（未使用合规 probe）")
                        # v2.2：阈值统一到 rules.json —— 常规升档只由 4 分触发，
                        # 3 分先追问或用 probe，消除 SKILL.md 与 09 决策表的口径冲突。
                        if d > 0 and act == "advance" and prev_score != ADVANCE_RULE["requires_score"]:
                            E(f"S18 第 {k + 1} 轮用 advance 升档，但上一轮仅 {prev_score} 分"
                              f"（常规升档须 {ADVANCE_RULE['requires_score']} 分）")
                        if d > 0 and act not in ("advance", "probe"):
                            E(f"S18 第 {k + 1} 轮难度升档却用「{act}」动作")
                        allowed = TRANSITIONS.get(prev_score, set())
                        if act in ACTIONS and allowed and act not in allowed and act != "start":
                            E(f"S18 第 {k + 1} 轮动作「{act}」与上一轮 {prev_score} 分的"
                              f"决策表不符（允许 {sorted(allowed)}）")
                        if d < 0 and prev_score > 1:
                            E(f"S18 第 {k} 轮 {prev_score} 分却在第 {k + 1} 轮降档")
                for k, trn in enumerate(turns):
                    dec = trn.get("decision") or {}
                    act = str(dec.get("action", ""))
                    if act not in ACTIONS:
                        E(f"S19 第 {k + 1} 轮 decision.action「{act}」非法")
                        continue
                    if act == "start" and k != 0:
                        E(f"S19 第 {k + 1} 轮不应使用 start（只有首轮可以）")
                    if trn.get("followup_of") and act != "followup":
                        E(f"S19 第 {k + 1} 轮标了 followup_of 却用「{act}」动作规避追问校验")
                    if k == 0:
                        continue
                    prev = turns[k - 1]
                    prev_ans = str(prev.get("answer", ""))
                    if act == "followup":
                        if str(trn.get("followup_of", "")) != str(prev.get("id", "")):
                            E(f"S19 第 {k + 1} 轮追问未指向上一轮 {prev.get('id')}")
                        based = str(dec.get("based_on") or "")
                        if len(based) < 8 or based not in prev_ans:
                            E(f"S19 第 {k + 1} 轮 based_on 不是上一轮回答的原文片段（≥8 字）")
                        if based and based not in str(trn.get("question", "")):
                            E(f"S19 第 {k + 1} 轮追问题干没有引用回答原句")
                    if act == "advance" and trn.get("followup_of"):
                        E(f"S19 第 {k + 1} 轮升档却标了 followup_of")
                    if act == "retreat" and not str(dec.get("reason", "")).strip():
                        E(f"S19 第 {k + 1} 轮降档未写 reason")
                    # S28：放宽口子必须严格合规，伪造 probe 一律拦下。
                    if act == "probe":
                        ok_probe, why = probe_check(turns, k, lv)
                        if not ok_probe:
                            E(f"S28 第 {k + 1} 轮试探性升档不合规：{why}")
                for k, turn in enumerate(turns[:len(ms)]):
                    mid = ms[k][0]
                    if doc_levels.get(mid) != str(turn.get("level", "")):
                        E(f"S20 M{mid} 难度标签与台账 level 不一致")
                track_rows = [ln for ln in body8
                              if re.match(r"^\|\s*\d+\s*\|\s*L[1-4]\s*\|", ln.strip())]
                if len(track_rows) != len(ms):
                    E(f"S20 难度轨迹表 {len(track_rows)} 行，与 {len(ms)} 轮不一致")
                for k, ln in enumerate(track_rows[:len(turns)]):
                    want_lv = str(turns[k].get("level", ""))
                    act = str((turns[k].get("decision") or {}).get("action", ""))
                    label = ACTION_LABEL.get(act, "")
                    if want_lv and want_lv not in ln:
                        E(f"S20 难度轨迹第 {k + 1} 行难度与台账不一致")
                    if label and label not in ln:
                        E(f"S20 难度轨迹第 {k + 1} 行动作与 decision「{act}」不一致")
                scores = [int(t.get("score", -1)) for t in turns]
                top = max((LEVEL_NUM.get(x, 0) for x in lv), default=0)
                info["max_level"] = "L%d" % top if top else None
                end_key = ended_reason_key(interview_log)
                early = end_key in EARLY_EXEMPT
                legacy = interview_log.get("pick_group") is None
                info["early_stop"] = early
                info["ended_reason"] = end_key
                lo_r, hi_r = SESSION_RULE["target_rounds"][t]
                if end_key == "unknown":
                    W("S32 台账 ended_reason 未声明，按未正常完成处理")
                # S32：只有正常完成才校验目标轮数；提前结束不得倒逼补写后续轮次。
                if end_key == "completed" and not (lo_r <= len(turns) <= hi_r):
                    msg = (f"S32 ended_reason=completed 但只有 {len(turns)} 轮，目标 {lo_r}~{hi_r} 轮；"
                           "提前结束须标 user_stopped / capability_unavailable / failed")
                    W(msg) if legacy else E(msg)
                if early and not turns:
                    W("S32 0 轮取消：章 8 须写明未发生问答，不得补写轮次")
                # 「出现 4 分必须到 L3」改成「若还有下一轮，动作符合决策表」；
                # 最高难度是观察结果，不是不顾实际回答的交付指标（I02）。
                for k, sc in enumerate(scores[:-1]):
                    if sc != ADVANCE_RULE["requires_score"]:
                        continue
                    nxt = turns[k + 1]
                    act = str((nxt.get("decision") or {}).get("action", ""))
                    now = LEVEL_NUM.get(lv[k], 0)
                    if now >= 4:
                        continue  # 已到 L4，保持即可
                    if act != "advance":
                        E(f"S21 第 {k + 1} 轮 {sc} 分且还有下一轮，下一轮动作应为 advance（实际「{act}」）")
                    if LEVEL_NUM.get(lv[k + 1], 0) <= now:
                        E(f"S21 第 {k + 1} 轮 {sc} 分后有升档机会，难度却未提升")
                if (not early and max(scores, default=-1) >= 3
                        and len(turns) >= lo_r and top < 2):
                    E("S21 完成整场且有 3 分以上，最高难度仍停在 L1")
                run = 0
                for trn in turns:
                    run = run + 1 if str((trn.get("decision") or {}).get("action", "")) == "followup" else 0
                    if run > FOLLOWUP_RULE["consecutive_max"]:
                        E(f"S21 连续追问超过 {FOLLOWUP_RULE['consecutive_max']} 次")
                        break
                n_follow = sum(1 for trn in turns
                               if str((trn.get("decision") or {}).get("action", "")) == "followup")
                if n_follow > FOLLOWUP_RULE["session_max"]:
                    E(f"S21 全场追问 {n_follow} 次，超过上限 {FOLLOWUP_RULE['session_max']}")
                # v2.1：连续 2 次同档追问且仍有话可说时，必须按 09 §3.1 试探性升一档。
                # 用户提前结束 / v1 老台账只给 warn，避免倒逼伪造后续轮次。
                for k in range(3, len(turns)):
                    a, b, cur = turns[k - 1], turns[k - 2], turns[k]
                    lv_a = str(a.get("level", ""))
                    if (str((a.get("decision") or {}).get("action", "")) == "followup"
                            and str((b.get("decision") or {}).get("action", "")) == "followup"
                            and str(b.get("level", "")) == lv_a
                            and int(a.get("score", -1)) in tuple(PROBE_RULE["score_range"])
                            and str(cur.get("level", "")) == lv_a
                            and str((cur.get("decision") or {}).get("action", "")) != "retreat"
                            and LEVEL_NUM.get(lv_a, 4) < 4):
                        msg = (f"S21 第 {k + 1} 轮前已连续 2 次同档追问且得分 {a.get('score')}，"
                               "须按 09 §3.1 试探性升一档（action=probe）")
                        W(msg) if (legacy or early) else E(msg)
    else:
        if "8" in bounds:
            E("S13 紧凑版不得出现章 8")
        if interview_log is not None:
            E("S16 紧凑版不得提供 interview-log（未进行模拟面试）")
    info["interview_rounds"] = len(ms)

    # ---- v2.2 S30 每题固定字段（D02）：删掉框架 / 意图 / 优先级不能静默通过 ----
    for q in qs:
        need = QUESTION_FIELDS.get(q["ch"])
        if not need:
            continue
        end = min(x for x in stops if x > q["line"])
        block = [ln.strip() for ln in lines[q["line"]:end]]
        for fld in need:
            if not any(ln.startswith(fld) for ln in block):
                E(f"S30 Q{q['ch']}-{q['num']} 缺少固定字段「{fld}」")

    # ---- v2 S22 参考答案必须是「没有问题的真实回答」，不是框架 / 占位 ----
    for q in qs:
        if q["ch"] not in (3, 4, 5, 6):
            continue
        end = min(x for x in stops if x > q["line"])
        block = lines[q["line"]:end]
        ref = next((ln for ln in block if ln.strip().startswith("**参考答案**")), "")
        if not ref:
            E(f"S22 Q{q['ch']}-{q['num']} 缺少「参考答案」行")
            continue
        body = ref.split("：", 1)[-1] if "：" in ref else ref
        n = effective_chars([body])
        if n < REF_ANSWER_MIN:
            E(f"S22 Q{q['ch']}-{q['num']} 参考答案仅 {n} 字，不是完整真实回答（≥{REF_ANSWER_MIN}）")
        if PLACEHOLDER_RE.search(body):
            E(f"S22 Q{q['ch']}-{q['num']} 参考答案含占位词，未给出真实回答")
        if len(STEP_RE.findall(body)) < 2:
            E(f"S22 Q{q['ch']}-{q['num']} 参考答案没有编号分点，只是空泛框架")
        if not EVIDENCE_ANCHOR.search(body):
            W(f"S22 Q{q['ch']}-{q['num']} 参考答案缺可核对的证据锚")
        if not COST_ANCHOR.search(body):
            W(f"S22 Q{q['ch']}-{q['num']} 参考答案没写代价 / 边界 / 替代方案")
        # S34 形式护栏：教学代码不是候选人经历。真实性仍须人工确认（06/07）。
        if resume_state != "real" and "code/" in body and re.search(
                r"我(做过|实现过|负责过|在项目里)|我们项目里|我过往项目", body):
            W(f"S34 Q{q['ch']}-{q['num']} 把教学代码写成第一人称经历；"
              "教学示例应写「这个演示实现中」，方案写「如果由我实现，我会」")

    # ---- v2 S23 交互版每轮必须给出该题的满分示范作答 ----
    if mode == "交互版" and "8" in bounds:
        _s8, _e8 = bounds["8"]
        b8 = lines[_s8 + 1:_e8]
        positions = [(i, mm) for i, ln in enumerate(b8)
                     if (mm := M_RE.match(ln.strip()))]
        for k, (pos, _mm) in enumerate(positions):
            end = positions[k + 1][0] if k + 1 < len(positions) else len(b8)
            grp = b8[pos:end]
            demo = next((ln for ln in grp if ln.strip().startswith("**示范作答**")), "")
            if not demo:
                E(f"S23 第 {k + 1} 轮缺少「示范作答」（这题没有问题的真实回答）")
                continue
            body = demo.split("：", 1)[-1] if "：" in demo else demo
            n = effective_chars([body])
            if n < MODEL_ANSWER_MIN:
                E(f"S23 第 {k + 1} 轮示范作答仅 {n} 字，未给出完整正确回答（≥{MODEL_ANSWER_MIN}）")
            if PLACEHOLDER_RE.search(body):
                E(f"S23 第 {k + 1} 轮示范作答含占位词")
            ans_line = next((ln for ln in grp if ln.strip().startswith("**回答**")), "")
            ans_body = ans_line.split("：", 1)[-1].strip() if "：" in ans_line else ""
            if ans_body and ans_body == body.strip():
                E(f"S23 第 {k + 1} 轮示范作答与用户回答完全相同，等于没给正确答案")
            if not EVIDENCE_ANCHOR.search(body) or not COST_ANCHOR.search(body):
                W(f"S23 第 {k + 1} 轮示范作答缺证据锚或代价 / 边界")

    # ---- v2 S24 章 4「简历针对性修改清单」----
    r_head = next((i for i, ln in enumerate(lines)
                   if RESUME_LIST_HEAD.match(ln.strip())), None)
    if r_head is None:
        E("S24 章 4 缺少「### 简历针对性修改清单」小节")
    else:
        r_end = len(lines)
        for j in range(r_head + 1, len(lines)):
            if re.match(r"^#{2,3}\s", lines[j]) or Q_RE.match(lines[j].strip()):
                r_end = j
                break
        seg = lines[r_head + 1:r_end]
        rows = [ln.strip() for ln in seg if re.match(r"^\d+\.\s", ln.strip())]
        if resume_state == "real":
            if len(rows) < 3:
                E(f"S24 已提供真实简历，修改清单只有 {len(rows)} 条（须 ≥3 条）")
            for k, ln in enumerate(rows, 1):
                if "**改法**" not in ln or "**对应 JD**" not in ln:
                    E(f"S24 修改清单第 {k} 条缺「**改法**」或「**对应 JD**」")
        elif resume_state in ("placeholder", "none"):
            txt = "".join(seg)
            if "需替换为真实简历" not in txt and "未提供简历" not in txt:
                E("S24 占位 / 未提供简历时，清单须写明「需替换为真实简历」或「未提供简历」")

    # ---- v2 S25 / S26 章 3「JD 题型映射」：不同 JD 必须分配不同题型 ----
    j_head = next((i for i, ln in enumerate(lines)
                   if PROFILE_MAP_HEAD.match(ln.strip())), None)
    basics_qids = []
    if j_head is None:
        E("S25 章 3 缺少「### JD 题型映射」小节")
    else:
        j_end = len(lines)
        for j in range(j_head + 1, len(lines)):
            if re.match(r"^#{2,3}\s", lines[j]) or Q_RE.match(lines[j].strip()):
                j_end = j
                break
        seg = lines[j_head + 1:j_end]
        fp = next((mm for ln in seg if (mm := JD_FP_RE.match(ln.strip()))), None)
        if fp is None:
            E("S25 映射小节缺少「> JD 指纹：…」行")
        else:
            parts = [p.strip() for p in fp.group(1).split("｜")]
            kws = ([x.strip() for x in re.split(r"[/、,，]", parts[2]) if x.strip()]
                   if len(parts) >= 3 else [])
            if len(parts) < 4 or len(kws) < 3:
                E("S25 JD 指纹格式须为「公司｜岗位｜≥3 个关键词（/ 分隔）｜面试类型」")
            info["jd_keywords"] = kws
        tbl = []
        for ln in seg:
            s = ln.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) != 4:
                E(f"S25 映射表行不是 4 列：{s[:50]}")
                continue
            if cells[0] == "题域" or set(cells[0]) <= set("-: "):
                continue
            tbl.append(cells)
        if not tbl:
            E("S25 映射表没有有效数据行")
        ch3_qids = {f"Q3-{q['num']}" for q in qs if q["ch"] == 3}
        seen, doms, basics_n = [], set(), 0
        for dom, kwd, qty, qids_cell in tbl:
            if dom not in DOMAINS:
                E(f"S25 未知题域「{dom}」，须在 {sorted(DOMAINS)} 内")
            doms.add(dom)
            ids = re.findall(r"Q3-\d+", qids_cell)
            if not qty.isdigit() or int(qty) != len(ids):
                E(f"S25 题域 {dom} 题量「{qty}」与列出的 Q 编号数 {len(ids)} 不一致")
            seen.extend(ids)
            if dom == "basics":
                basics_qids.extend(ids)
                basics_n += len(ids)
        if len(doms) < 2:
            E("S25 映射表只覆盖 1 个题域，题型未按 JD 分配")
        if len(seen) != len(set(seen)):
            E("S25 映射表里 Q 编号重复分配到多个题域")
        if set(seen) != ch3_qids:
            E(f"S25 映射表 Q 编号与章 3 实际题目不一致：表 {sorted(set(seen))} / 正文 {sorted(ch3_qids)}")
        min_b = BASICS_MIN[t]
        if basics_n < min_b:
            E(f"S25 基础八股题 {basics_n} 道，少于 {t} 下限 {min_b}")
        info["jd_domains"] = sorted(doms)
        info["basics_questions"] = basics_n
        for qid in basics_qids:
            hit = next((x for x in qs if f"Q{x['ch']}-{x['num']}" == qid), None)
            if hit and "【基础" not in hit["title"]:
                E(f"S26 {qid} 归入基础八股题域，题目标题却没有「【基础八股】」标记")

    # ---- v2 S27 对话框固定选项与「只选选项无文本」----
    if mode == "交互版" and interview_log is not None:
        for k, trn in enumerate(interview_log.get("turns", []), 1):
            opt = str(trn.get("option", "")).strip()
            if not opt:
                E(f"S27 第 {k} 轮台账缺少 option 字段（无法核对对话框选项）")
                continue
            if "口述" in opt:
                E(f"S27 第 {k} 轮出现「口述作答」占位选项，v2 已禁用")
            if opt not in OPTION_WHITELIST:
                E(f"S27 第 {k} 轮选项「{opt}」不在固定白名单 {sorted(OPTION_WHITELIST)}")
                continue
            ans = str(trn.get("answer", "")).strip()
            answered = bool(trn.get("answered", bool(ans)))
            if opt == "这题我不会":
                if answered or int(trn.get("score", -1)) != 0:
                    E(f"S27 第 {k} 轮选「这题我不会」却记为有效作答 / 非 0 分")
            elif not ans:
                E(f"S27 第 {k} 轮选「{opt}」但没有任何文本，不能算回答（须重问同题）")
            elif not answered:
                E(f"S27 第 {k} 轮有回答文本却标 answered=false")

    return {"file": base, "interview_type": t, "info": info,
            "errors": errors, "warnings": warnings, "passed": not errors}


def _pad_chapters(text, t="业务面"):
    """把回归样例的占位文字按 03 §3 的章节预算分散到各章标题之后。

    早期版本把 6,500 字全塞进章 1 来凑总字数，属于「注水」；S6 收紧后
    该样例自己就不合格了。占位文字插在章标题之后、第一个 Q 行之前，
    因此不会进入任何单题块、不影响 S5。
    """
    filler = ("公司在本季度持续推进产品线与业务指标建设，该岗位需要同时理解业务流程、"
              "系统链路与协作边界，并把目标拆解为可验证的阶段结果，因此调研时优先关注"
              "产品定位、收入来源、近十二个月动态与组织分工。")
    unit = effective_chars([filler])
    lines = text.splitlines()
    secs = parse_sections(lines)
    plan = []
    for pos, (tok, start) in enumerate(secs):
        if tok == "附录":
            continue
        end = secs[pos + 1][1] if pos + 1 < len(secs) else len(lines)
        n = effective_chars(lines[start:end])
        blo, bhi = budget_of(int(tok), t)
        k = max(0, round(((blo + bhi) // 2 - n) / unit))
        plan.append((start + 1, k))
    for idx, k in reversed(plan):
        if k:
            lines[idx:idx] = [filler] * k
    return "\n".join(lines)


def build_good():
    """v2 合规夹具：头部简历状态、章 3 JD 题型映射、章 4 简历修改清单、
    每题完整真实参考答案。供 --self-test 与真实文档对照。"""
    L = [
        "# 示例公司 · AI Agent 工程师 面试准备",
        "> 面试类型：业务面（一面）｜生成日期：2026-01-04｜信息来源：公开资料 + 用户简历",
        "> 面经检索：未检索到有效面经",
        "> 简历状态：已解析（真实简历）",
        "> 模式：紧凑版",
        "",
        "## 0｜使用说明与目录",
        "本文件按一场业务面组织。**结论先行**，先看 🔴 必背 与加粗句。推荐阅读顺序 1 → 2 → 3 → 4 → 6。",
        "样式图例：🔴 必背 表示必须记住，🔵 加分 表示有余力再看；引用块是补充提示，来源统一放文末附录。",
        "元信息：公司 示例公司，岗位 AI Agent 工程师，面试类型 业务面（一面），作品集 未提供，面经 未检索到有效面经。",
        "各项内容均以简历与 JD 为准，公司业务信息以公开资料为准，未核实处已标注。",
        "",
        "## 1｜公司与岗位调研",
        "公司主营业务与商业模式见官方资料 [1]。该岗位在组织中负责把业务目标拆成可执行的方案与数据口径。",
        "",
        "## 2｜自我介绍与朗读稿",
        "**版本 A（1 分钟）**：我是一名做 Agent 工程的候选人 / 停顿 / 主要做过最小循环与工具参数校验两件事。",
        "**版本 B（3~5 分钟）**：从结论说起，我的能力主张是能把不确定的多步任务落成可观测的闭环。",
        "演示实现里，code/01_minimal_react_agent.py 做了消息状态、解析校验、观察回填与停止条件；"
        "code/02_function_calling.py 做了 Schema 校验、错误回填与有限重试（教学示例，非候选人履历）。",
        "与该公司该岗位的匹配在于 JD 强调的运行时工程与可靠性，这份演示正好覆盖这两块，动机是继续把 Agent 做到可生产。",
        "",
    ]
    # 一行写全「结论 → 编号分点 → 证据 → 代价与边界」，S22 按单行解析。
    ref = ("**参考答案**：**结论先行**：路径可枚举就用固定流程，只有决策点无法预先枚举才上 Agent。"
           "1. 依据来自运行时状态与观察回填，模型决策的价值在处理未知分支。"
           "2. 证据：演示实现里用 code/01_minimal_react_agent.py 跑通最小循环，并用日志回放把死循环定位到具体一步。"
           "3. 代价与边界：多步决策让错误沿轨迹传播，延迟与 token 成本上升；高合规动作仍交状态机，"
           "失败退化为人工确认，代价是多一次交互。")
    ref_short = ("**参考答案**：**结论先行**：先给边界再给做法。"
                 "1. 依据来自职责划分与提交记录，结论要有可核对出处。"
                 "2. 证据：演示实现里用 code/02_function_calling.py 做 Schema 校验，日志能定位到失败的那一步。"
                 "3. 代价与边界：细节说满会暴露没参与的模块，替代方案是明确只讲自己负责的那段，"
                 "涉及他人负责的模块只给接口口径，不替别人下结论。")
    basics = ["【基础八股】LLM 的自回归生成与训练推理差异",
              "【基础八股】Transformer 自注意力与 QKV 的平方复杂度",
              "【基础八股】PPO 在 RLHF 里的 clip 与 KL 约束"]
    titles = {4: "项目里你的职责边界到哪", 5: "讲一次最大困难及解决过程",
              6: "你没有这方面经验为什么能胜任",
              7: "这个岗位入职前三个月最希望解决什么"}

    def q_title(ch, k):
        if ch == 3:
            return basics[k - 1] if k <= 3 else f"怎么判断该用 Agent 还是 Workflow（{k}）"
        return f"{titles[ch]}（{k}）"

    for ch, n in [(3, 8), (4, 6), (5, 4), (6, 1), (7, 4)]:
        L.append(f"## {ch}｜" + {3: "业务能力考察", 4: "简历深挖", 5: "行为面试与个人特质",
                                  6: "Gap 质疑题", 7: "反问问题"}[ch])
        if ch == 3:
            L += [
                "### JD 题型映射", "",
                "> JD 指纹：示例公司｜AI Agent 工程师｜Agent / 工具调用 / 评估 / 运行时｜业务面", "",
                "| 题域 | JD 关键词 | 题量 | 对应 Q 编号 |",
                "|---|---|---|---|",
                "| basics | LLM / Transformer / PPO | 3 | Q3-1, Q3-2, Q3-3 |",
                "| agent | Agent / 工具调用 / 运行时 | 3 | Q3-4, Q3-5, Q3-6 |",
                "| eval | 评估 / 可观测 | 2 | Q3-7, Q3-8 |",
                "",
            ]
        if ch == 4:
            L += [
                "### 简历针对性修改清单", "",
                "1. **位置**：项目经历第 1 条 → **问题**：只写实现功能，没有结果 → "
                "**改法**：补上 Schema 校验与有限重试后的成功率变化 → **对应 JD**：工具调用 / 运行时可靠性",
                "2. **位置**：技能清单 → **问题**：列了十个框架名，与 JD 关键词不重合 → "
                "**改法**：改成 JD 原词（工具调用、状态机、可观测），每项附一个本项目文件 → **对应 JD**：Agent 工程",
                "3. **位置**：自我评价 → **问题**：写「学习能力强」无证据 → "
                "**改法**：换成轨迹回放定位死循环的一次复盘 → **对应 JD**：故障定位 / 评估",
                "",
            ]
        for k in range(1, n + 1):
            L.append(f"**Q{ch}-{k}｜{q_title(ch, k)}**")
            if ch == 7:
                L.append("**为什么问**：想确认岗位的真实挑战与成功标准，同时传递已研究过业务。")
                L.append("**优先级**：🔵 加分")
            else:
                L.append("**考察意图**：核对是否理解结论与依据，而不是背名词。")
                L.append("**答题框架**：1. 一句话结论 2. 原理与动机 3. 项目做法 4. 代价与替代方案。")
                L.append(ref_short if ch in (4, 5) else ref)
                L.append("**优先级**：🔴 必背")
            L.append("")
    L += [
        "## 附录｜参考来源",
        "",
        "### 公司资料",
        "1. [示例公司官网业务介绍](https://example.com/about) — 官网，2026-10-01",
        "",
        "### 面经",
        "（未检索到有效面经）",
    ]
    return _pad_chapters("\n".join(L))


def sample_interview_log(with_probe=False):
    """6 轮合规轨迹：L1 八股池随机首题 → 追问 → 升 L2 → 追问 → 升 L3 → 追问。
    每次升档都由上一轮 4 分触发；追问的 based_on 都是上一轮回答原文片段。
    v2：每轮带固定 option，首题由 pick_group=basics 从八股池复算。
    v2.1：with_probe=True 生成一条「连续 2 次同档追问 → 试探性升档」的合规轨迹。
    """
    bank = load_bank()
    seed = "20261104-demo"
    first_id = reproduce_pick(bank, seed, "L1", "basics") if bank else "L1-B01"
    bank_items = {}
    if bank is not None:
        for _d in bank["levels"].values():
            for _x in _d["items"]:
                bank_items[_x["id"]] = _x
    base_rows = [
        ("M1", "Q3-1", 1, None, "L1", first_id, "LLM 的自回归生成与训练推理差异",
         "大语言模型为什么按 next-token prediction 训练、按自回归方式生成？训练和推理的不一致会带来什么问题？",
         "训练时用 teacher forcing，每一步都喂真实前缀；推理时只能喂自己生成的前缀，这叫 exposure bias，"
         "早期错一个 token 后面会沿错误前缀越走越偏。项目里做多步生成时用轨迹日志定位第一个偏掉的 token。",
         True, 3, "有依据缺代价", "start", None, "题库 L1 八股池随机首题"),
        ("M2", "Q3-1", 2, "M1", "L1", first_id, "训练推理不一致的代价",
         "你刚才说『训练时用 teacher forcing，每一步都喂真实前缀』，那推理时早期错一个 token，除了沿前缀累积还有什么代价？怎么缓解？",
         "长序列会越走越偏；可以用 scheduled sampling 在训练里混入模型自己的前缀，代价是训练不稳定、收敛变慢；"
         "也可以用约束解码兜底，但会牺牲生成多样性。项目里只在高风险字段上开约束。",
         True, 4, "完整可讲", "followup",
         "训练时用 teacher forcing，每一步都喂真实前缀", "回答缺代价与替代方案，原地追问"),
        ("M3", "Q3-4", 3, None, "L2", "L2-02", "工具报错后的错误回填",
         "工具返回格式错误时，直接重试和把错误回填给模型，各自代价是什么？",
         "直接重试会重复消耗 token 并可能重复触发副作用；回填错误让模型能修参数，但要多一轮推理。我们用 Schema 校验加有限重试。",
         True, 3, "有依据缺代价", "advance", None, "上一轮 4 分，换考点升一档"),
        ("M4", "Q3-4", 4, "M3", "L2", "L2-02", "已执行一半的副作用",
         "你刚才说『Schema 校验加有限重试』，那已经执行了一半的工具调用怎么保证不重复？",
         "用幂等键把动作与结果绑定，重放时先查键再决定是否真的执行，恢复后不会重复扣款；边界是本地模拟没有真实支付系统。",
         True, 4, "完整可讲", "followup", "Schema 校验加有限重试", "追问幂等与恢复"),
        ("M5", "Q6-1", 5, None, "L3", "L3-01", "轨迹回放定位死循环",
         "线上一个 Agent 反复调用同一个工具停不下来，给你一份轨迹日志，你怎么定位到具体哪一步？",
         "按状态、动作、观察三段比对重复模式，先找第一次出现完全相同三元组的轮次，再回放前一轮的观察；代价是日志压缩后会丢原文。",
         True, 3, "有依据缺代价", "advance", None, "上一轮 4 分，升到故障定位"),
        ("M6", "Q6-1", 6, "M5", "L3", "L3-01", "压缩后的可回溯性",
         "你刚才说『日志压缩后会丢原文』，那怎么在压缩下仍然定位到被丢掉的那条约束？",
         "压缩前列不可丢清单并保留原文引用，压缩后做一致性校验，不一致就回滚到未压缩版本；代价是多一份存储。",
         True, 3, "有依据缺代价", "followup", "日志压缩后会丢原文", "追问压缩边界与回滚"),
    ]
    # v2.1 放宽后的合规轨迹：L1 → 追问 → 追问 → probe 升 L2 → 追问 → 追问。
    # M4 的 based_on 是 M3 回答原句并原样出现在题干，reason 写明「试探」，
    # 换考点换 bank_id，升幅恰好 +1；全场只用 1 次 probe。
    probe_rows = [
        ("M1", "Q3-1", 1, None, "L1", first_id, "LLM 的自回归生成与训练推理差异",
         "大语言模型为什么按 next-token prediction 训练、按自回归方式生成？训练和推理的不一致会带来什么问题？",
         "训练时用 teacher forcing，每一步都喂真实前缀；推理时只能喂自己生成的前缀，这叫 exposure bias，"
         "早期错一个 token 后面会沿错误前缀越走越偏。项目里做多步生成时用轨迹日志定位第一个偏掉的 token。",
         True, 2, "有结论无依据", "start", None, "题库 L1 八股池随机首题"),
        ("M2", "Q3-1", 2, "M1", "L1", first_id, "训练推理不一致的代价",
         "你刚才说『训练时用 teacher forcing，每一步都喂真实前缀』，那除了 exposure bias，"
         "训练与推理不一致还有哪些代价，怎么缓解？",
         "长序列会越走越偏；可以用 scheduled sampling 在训练里混入模型自己的前缀，代价是训练不稳定、收敛变慢；"
         "也可以用约束解码兜底，但会牺牲生成多样性。项目里只在高风险字段上开约束。",
         True, 2, "有结论无依据", "followup",
         "训练时用 teacher forcing，每一步都喂真实前缀", "回答缺依据，原地追问"),
        ("M3", "Q3-1", 3, "M2", "L1", first_id, "缓解手段自身的偏差",
         "你刚才说『可以用 scheduled sampling 在训练里混入模型自己的前缀』，"
         "那这个缓解手段本身会引入什么新的偏差，为什么不是免费午餐？",
         "混入模型前缀会让训练分布随模型变化而漂移，早期容易学到自己的错误并放大；"
         "要调混入比例并在验证集上盯困惑度，替代方案是只在高风险字段用约束解码。项目里用小比例混入加固定种子做回归。",
         True, 2, "有结论无依据", "followup",
         "可以用 scheduled sampling 在训练里混入模型自己的前缀", "连续第 2 次同档追问"),
        ("M4", "Q3-4", 4, None, "L2", "L2-02", "工具报错后的错误回填",
         "你刚才说『要调混入比例并在验证集上盯困惑度』，换个考点：工具返回格式错误时，"
         "直接重试和把错误回填给模型，各自代价是什么？",
         "直接重试会重复消耗 token 并可能重复触发副作用；回填错误让模型能修参数，但要多一轮推理。"
         "我们用 Schema 校验加有限重试。",
         True, 2, "有结论无依据", "probe",
         "要调混入比例并在验证集上盯困惑度",
         "试探性升档：连续 2 次同档追问仍 2 分，换考点升一档"),
        ("M5", "Q3-4", 5, "M4", "L2", "L2-02", "已执行一半的副作用",
         "你刚才说『Schema 校验加有限重试』，那已经执行了一半的工具调用怎么保证不重复？",
         "用幂等键把动作与结果绑定，重放时先查键再决定是否真的执行，恢复后不会重复扣款；"
         "边界是本地模拟没有真实支付系统。",
         True, 3, "有依据缺代价", "followup", "Schema 校验加有限重试", "追问幂等与恢复"),
        ("M6", "Q3-4", 6, "M5", "L2", "L2-02", "压缩后的可回溯性",
         "你刚才说『用幂等键把动作与结果绑定』，那压缩后的日志还能不能定位到被丢掉的那条约束？",
         "压缩前列不可丢清单并保留原文引用，压缩后做一致性校验，不一致就回滚到未压缩版本；代价是多一份存储。",
         True, 3, "有依据缺代价", "followup", "用幂等键把动作与结果绑定", "追问压缩边界与回滚"),
    ]
    rows = probe_rows if with_probe else base_rows
    keys = ("id", "q_ref", "round", "followup_of", "level", "bank_id", "topic",
            "question", "answer", "answered", "score", "verdict",
            "action", "based_on", "reason")
    log = {
        "session_id": "2026-01-04-demo",
        "mode": "interactive",
        "started_at": "2026-01-04",
        "pick_seed": seed,
        "pick_group": "basics",
        "ended_reason": "达到目标轮数（业务面 6 轮）",
        "turns": [
            {k: v for k, v in dict(zip(keys, r)).items()
             if k not in ("action", "based_on", "reason")} | dict(
                 decision={"action": r[12], "based_on": r[13], "reason": r[14]},
                 gaps=["失败代价与替代方案"],
                 fix="结论先行 → 依据 → 项目证据 → 代价与边界，四步补齐。",
                 domain_ref=(bank_items.get(r[5], {}).get("kb_ref")
                             or "INTERVIEW.md（题号待核对）"))
            for r in rows
        ],
    }
    for i, turn in enumerate(log["turns"]):
        turn["option"] = "需要提示" if i == 3 else "直接作答"
    return log


def build_interactive(log=None):
    d = build_good().replace("> 模式：紧凑版", "> 模式：交互版")
    if log is None:
        log = sample_interview_log()
    label = {"start": "题库随机首题", "advance": "升档：换考点", "followup": "追问：补依据",
             "hold": "保持：换考点", "retreat": "降档：改问场景",
             "probe": "试探升档：换考点"}
    top = max(LEVEL_NUM[str(t["level"])] for t in log["turns"])
    followups = sum(1 for t in log["turns"] if t["decision"]["action"] == "followup")
    L = [
        "## 8｜模拟面试记录与评判",
        "",
        "> 模式：交互版｜共 %d 轮（含 %d 次追问）｜最高难度 L%d｜评分 0~4｜首题种子 %s"
        % (len(log["turns"]), followups, top, log.get("pick_seed", "")),
        "",
        "### 难度轨迹",
        "",
        "| 轮次 | 难度 | 触发动作 | 得分 |",
        "|---|---|---|---|",
    ]
    for i, t in enumerate(log["turns"], 1):
        L.append("| %d | %s | %s | %d |" %
                 (i, t["level"], label[t["decision"]["action"]], t["score"]))
    L.append("")
    def demo(k):
        return ("**结论先行**：这题的正确答法是先给可验证的结论，再把依据、项目证据与代价补齐。"
                "1. 依据来自机制而不是名词，第 %d 轮考的正是「为什么这样设计」这一层。"
                "2. 证据：演示实现里用 code/01_minimal_react_agent.py 跑通最小循环，并用轨迹日志定位死循环，"
                "在 code/02_function_calling.py 里做 Schema 校验与有限重试的回归（教学示例，非本人履历）。"
                "3. 代价与边界：多一层校验会多一次延迟与 token 开销；替代方案是把高危动作交给状态机，"
                "失败时退化为人工确认，边界是不适用于低风险只读操作。" % k)

    for i, t in enumerate(log["turns"], 1):
        ans = t["answer"] if t["answered"] else "未作答"
        L += [
            "**%s｜对应 %s｜第 %d 轮｜%s**" % (t["id"], t["q_ref"], t["round"], t["level"]),
            "**回答**：%s" % ans,
            "**评分**：%d / 4｜%s" % (t["score"], t["verdict"]),
            "**失分点**：回答里没有讲清失败代价与替代方案，追问时也说不出边界条件。",
            "**改进口径**：结论先行，再给原理，接着用项目文件与日志作证据，最后补代价与边界。",
            "**考点索引**：INTERVIEW.md 第 11 题 / code/01_minimal_react_agent.py",
            "**示范作答**：%s" % demo(i),
            "",
        ]
    L += [
        "### 总评表",
        "",
        "| 轮次 | 对应题 | 得分 | 最该补的一件事 |",
        "|---|---|---|---|",
    ]
    for t in log["turns"]:
        L.append("| %s | %s | %d | 补代价与替代方案 |" % (t["id"], t["q_ref"], t["score"]))
    L += [
        "",
        "本场最高难度 L%d，下一场建议从 L%d 起手。" % (top, max(1, top - 1)),
        "**下轮优先复习**：1. 第 4 题 Agent 与 Workflow 边界 2. 第 11 题 最小循环五件套 3. 第 92 题 改动后的回归验证",
        "",
    ]
    return d.replace("## 附录｜参考来源", "\n".join(L) + "\n## 附录｜参考来源")


def sample_evidence():
    return {
        "searched_at": "2026-01-04",
        "tools_used": ["web_search", "fetch_page"],
        "queries": ["示例公司 AI Agent 工程师 面经"],
        "found": True,
        "items": [
            {"platform": "牛客", "title": "面经 A", "date": "2025-11-12",
             "url": "https://www.nowcoder.com/discuss/a", "points": ["ReAct 循环", "工具参数校验"]},
            {"platform": "知乎", "title": "面经 B", "date": "2025-11-20",
             "url": "https://www.zhihu.com/question/b", "points": ["ReAct 循环", "上下文预算"]},
            {"platform": "脉脉", "title": "面经 C", "date": "2025-12-05",
             "url": "https://maimai.cn/article/c", "points": ["轮次与流程"]},
        ],
        "high_frequency_points": ["ReAct 循环"],
        # point_id → question_ids：高频标签只能打在关联题目上（E07）。
        "point_links": {"ReAct 循环": ["Q3-4"]},
    }


def build_with_evidence():
    d = build_good()
    d = d.replace("> 面经检索：未检索到有效面经",
                  "> 面经检索：找到 3 条（牛客 / 知乎 / 脉脉；2025-11-01 ~ 2026-01-01）")
    d = re.sub(r"^\*\*Q3-4｜.*\*\*$",
               "**Q3-4｜怎么判断该用 Agent 还是 Workflow（4）【面经高频】**",
               d, count=1, flags=re.M)
    d = d.replace("（未检索到有效面经）",
                  "2. [面经 A](https://www.nowcoder.com/discuss/a) — 牛客，2025-11-12\n"
                  "3. [面经 B](https://www.zhihu.com/question/b) — 知乎，2025-11-20\n"
                  "4. [面经 C](https://maimai.cn/article/c) — 脉脉，2025-12-05")
    return d


def self_test():
    good = validate(build_good(), None, "示例公司_AI Agent 工程师_面试准备.md")
    bad = build_good()
    bad = bad.replace("> 面经检索：未检索到有效面经",
                      "> 面经检索：找到 3 条（牛客 / 知乎；2025-11-01 ~ 2026-01-01）")
    bad = re.sub(r"^\*\*Q5-\d+｜.*\*\*$", "", bad, flags=re.M)
    bad = bad.replace("## 2｜自我介绍与朗读稿", "#### 不该有的四级标题\n\n## 2｜自我介绍与朗读稿")
    bad = bad.replace("公司主营业务与商业模式见官方资料 [1]。",
                      "公司主营业务与商业模式见官方资料 [1] 与 [9]。")
    bad += "\n据面经，【面经高频】这道题经常出现。\n|a|b|c|d|e|\n"
    bad_r = validate(bad, None, "示例_面试准备.md")

    ev = sample_evidence()
    ev_doc = build_with_evidence()
    ev_r = validate(ev_doc, ev, "示例_面试准备.md")
    bad_ev = dict(ev)
    bad_ev["high_frequency_points"] = ["ReAct 循环", "轮次与流程"]
    bad_ev_r = validate(ev_doc, bad_ev, "示例_面试准备.md")

    # 题块排除章级引用块后，仍必须能拦住「答案正文真写超」的违规。
    long_q = build_good().replace(
        "**参考答案**：**结论先行**：路径可枚举就用固定流程",
        "**参考答案**：**结论先行**：" + "补" * 400 + "路径可枚举就用固定流程", 1)
    long_r = validate(long_q, None, "示例_面试准备.md")

    log = sample_interview_log()
    inter = build_interactive()
    inter_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", log)
    no_log_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", None)
    forged = json.loads(json.dumps(log))
    forged["turns"][0]["score"] = 4
    forged["turns"][0]["answer"] = "编造出来的回答"
    forged_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", forged)
    unans_log = json.loads(json.dumps(log))
    unans_log["turns"][3]["answer"] = ""
    unans_log["turns"][3]["answered"] = False
    unans_log["turns"][3]["score"] = 0
    unans_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", unans_log)

    # 09 自适应难度：跳级 / 低分硬升档 / 伪造追问依据 / 缺难度轨迹表
    jump = json.loads(json.dumps(log))
    jump["turns"][2]["level"] = "L3"
    jump_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", jump)
    low_adv = json.loads(json.dumps(log))
    low_adv["turns"][1]["score"] = 1
    low_adv_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", low_adv)
    forged_based = json.loads(json.dumps(log))
    forged_based["turns"][1]["decision"]["based_on"] = "这句根本没说过的话"
    forged_based_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", forged_based)
    mislabel = json.loads(json.dumps(log))
    mislabel["turns"][1]["decision"]["action"] = "hold"
    mislabel_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", mislabel)
    no_track = "\n".join(ln for ln in inter.splitlines()
                         if not re.match(r"^\|\s*\d+\s*\|\s*L[1-4]\s*\|", ln))
    no_track_r = validate(no_track, None, "示例公司_AI Agent 工程师_面试准备.md", log)

    # ---- v2.1 放宽升档：合规 probe 必须放行，6 类伪造 / 偷懒用法必须拦下 ----
    plog = sample_interview_log(with_probe=True)
    pdoc = build_interactive(plog)
    probe_ok_r = validate(pdoc, None, "示例公司_AI Agent 工程师_面试准备.md", plog)

    def with_doc(case_log):
        return validate(build_interactive(case_log), None,
                        "示例公司_AI Agent 工程师_面试准备.md", case_log)

    p_low = json.loads(json.dumps(plog))
    p_low["turns"][2]["score"] = 1                       # 触发轮降到 1 分
    p_low_r = with_doc(p_low)
    p_streak = json.loads(json.dumps(plog))
    p_streak["turns"][2]["decision"]["action"] = "hold"  # 打断连续追问
    p_streak["turns"][2]["followup_of"] = None
    p_streak_r = with_doc(p_streak)
    p_reason = json.loads(json.dumps(plog))
    p_reason["turns"][3]["decision"]["reason"] = "随便升一档"
    p_reason_r = with_doc(p_reason)
    p_jump = json.loads(json.dumps(plog))
    p_jump["turns"][3]["level"] = "L3"                   # 试探升档跳级
    p_jump_r = with_doc(p_jump)
    p_adv = json.loads(json.dumps(plog))
    p_adv["turns"][3]["decision"]["action"] = "advance"  # 用 advance 伪装低分升档
    p_adv_r = with_doc(p_adv)
    p_lazy = json.loads(json.dumps(plog))
    p_lazy["turns"] = p_lazy["turns"][:4]                # 追满 2 次仍原地不动
    p_lazy["turns"][3].update({"level": "L1", "q_ref": "Q3-1",
                               "bank_id": plog["turns"][0]["bank_id"],
                               "followup_of": "M3", "score": 2})
    p_lazy["turns"][3]["decision"]["action"] = "followup"
    p_lazy["turns"][3]["decision"]["reason"] = "继续同档追问"
    p_lazy_r = with_doc(p_lazy)
    # 老 v1 台账（无 pick_group）只给 warn，不被新规误伤成 error。
    p_lazy_v1 = json.loads(json.dumps(p_lazy))
    p_lazy_v1.pop("pick_group", None)
    p_lazy_v1_r = with_doc(p_lazy_v1)

    # ---- v2 新增反例：真实参考答案 / 示范作答 / 简历清单 / JD 映射 / 对话框选项 ----
    good_text = build_good()
    no_ref_r = validate("\n".join(ln for ln in good_text.splitlines()
                                  if not ln.startswith("**参考答案**")),
                        None, "示例_面试准备.md")
    no_demo = "\n".join(ln for ln in inter.splitlines()
                        if not ln.startswith("**示范作答**"))
    no_demo_r = validate(no_demo, None, "示例公司_AI Agent 工程师_面试准备.md", log)
    no_resume = "\n".join(ln for ln in good_text.splitlines()
                          if not RESUME_LIST_HEAD.match(ln.strip()))
    no_resume_r = validate(no_resume, None, "示例_面试准备.md")
    no_map = "\n".join(ln for ln in good_text.splitlines()
                       if not PROFILE_MAP_HEAD.match(ln.strip()))
    no_map_r = validate(no_map, None, "示例_面试准备.md")
    bad_opt = json.loads(json.dumps(log))
    bad_opt["turns"][0]["option"] = "我要口述作答"
    bad_opt_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", bad_opt)
    opt_only = json.loads(json.dumps(log))
    opt_only["turns"][0]["answer"] = ""
    opt_only_r = validate(inter, None, "示例公司_AI Agent 工程师_面试准备.md", opt_only)
    bank = load_bank()
    basics_items = [x for x in bank["levels"]["L1"]["items"]
                    if x.get("group") == "basics"] if bank else []
    basics_topics = " ".join(x["topic"] for x in basics_items)
    bank_ok = (bank is not None and bank.get("version") == "2.1"
               and len(basics_items) >= 6
               and all(w in basics_topics for w in ("LLM", "Transformer", "PPO")))
    # 固定输入 → 固定抽中 ID 的金标准：抽题器与校验器共享 pick()，一旦实现被
    # 意外改动，这里的硬编码值能立刻发现（外部评测 §8 建议）。
    gold = [
        ("20261105-audit", "L1", "basics", "L1-B06"),
        ("20261104-demo", "L1", "basics", "L1-B01"),
        ("gold-1", "L2", "behavior", "L2-BH02"),
        ("gold-2", "L1", "behavior", "L1-BH03"),
    ]
    gold_ok = all(reproduce_pick(bank, s, lv, grp) == want
                  for s, lv, grp, want in gold)
    ref_errs, ref_checked = check_bank_references(bank)
    bank_ref_ok = not ref_errs

    # ---- 可靠性回归：回答保真 / 来源校验 / 提前结束 / 异常输入 ----
    FN = "示例公司_AI Agent 工程师_面试准备.md"

    def vr(doc, ev=None, lg=None, fn=FN):
        return validate(doc, ev, fn, lg)

    a0 = log["turns"][0]["answer"]
    tampered = inter.replace("**回答**：" + a0,
                             "**回答**：" + a0[:40] + "之后全部替换成从未说过的回答。", 1)
    answer_tamper_r = vr(tampered, lg=log)                                    # I05
    verdict_r = vr(inter.replace("**评分**：3 / 4｜有依据缺代价",
                                 "**评分**：3 / 4｜完整可讲", 1), lg=log)      # I04
    qref_log = json.loads(json.dumps(log))
    qref_log["turns"][0]["q_ref"] = "Q7-1"
    qref_r = vr(build_interactive(qref_log), lg=qref_log)                     # I03
    topic_log = json.loads(json.dumps(log))
    topic_log["turns"][0]["topic"] = "离职原因"
    topic_log["turns"][0]["question"] = "请谈谈你的离职原因？"
    topic_r = vr(build_interactive(topic_log), lg=topic_log)                   # I09
    irrelevant_r = vr(inter.replace(
        "**失分点**：回答里没有讲清失败代价与替代方案，追问时也说不出边界条件。",
        "**失分点**：用户说过不存在的观点，因此扣分。", 1), lg=log)             # I06

    ev_good, ev_doc = sample_evidence(), build_with_evidence()
    old_url = ev_good["items"][0]["url"]
    for spoof in ("https://www.nowcoder.com.attacker.example/discuss/a",
                  "https://attacker.example/?ref=nowcoder.com"):
        e = json.loads(json.dumps(ev_good))
        e["items"][0]["url"] = spoof
        r = vr(ev_doc.replace(old_url, spoof), e, fn="示例_面试准备.md")
        if "host_r" not in locals():
            host_r = r
        else:
            query_r = r
    e_future = json.loads(json.dumps(ev_good))
    e_future["items"][0]["date"] = "2099-10-01"
    future_r = vr(ev_doc, e_future, fn="示例_面试准备.md")                      # E03
    e_dup = json.loads(json.dumps(ev_good))
    e_dup["items"][1] = json.loads(json.dumps(e_dup["items"][0]))
    e_dup["items"][1]["platform"] = "牛客网"
    dup_r = vr(ev_doc, e_dup, fn="示例_面试准备.md")                            # E06
    tag_r = vr(ev_doc.replace("**Q3-1｜", "**Q3-1｜【面经高频】", 1),
               ev_good, fn="示例_面试准备.md")                                  # E07
    drift_r = vr(ev_doc.replace("[面经 A]", "[另一个完全不相符的标题]")
                 .replace("— 牛客，2025-11-12", "— 牛客，2025-10-01"),
                 ev_good, fn="示例_面试准备.md")                                 # E08
    window_r = vr(ev_doc.replace("（牛客 / 知乎 / 脉脉；2025-11-01 ~ 2026-01-01）", ""),
                  ev_good, fn="示例_面试准备.md")                               # E09

    e_bad_date = json.loads(json.dumps(ev_good))
    e_bad_date["items"][0]["date"] = "2026-99-99"
    bad_date_r = vr(ev_doc, e_bad_date, fn="示例_面试准备.md")                   # E04
    e_bad_item = json.loads(json.dumps(ev_good))
    e_bad_item["items"] = ["bad-item"]
    bad_item_r = vr(ev_doc, e_bad_item, fn="示例_面试准备.md")                   # E05
    bad_score_log = json.loads(json.dumps(log))
    bad_score_log["turns"][0]["score"] = "not-a-number"
    bad_score_r = vr(inter, lg=bad_score_log)                                   # I08

    # 日期未知哨兵（02 §5 允许页面未标注发布日期）：预检早退会让后续检测全部失效。
    # 这里同时验：合法放行 / 附录篡改仍被 S9 拦 / 非法日期仍报 S0。
    e_undated = json.loads(json.dumps(ev_good))
    e_undated["items"][0]["date"] = "未标注"
    undated_doc = ev_doc.replace("— 牛客，2025-11-12", "— 牛客，发布日期未标注", 1)
    undated_r = vr(undated_doc, e_undated, fn="示例_面试准备.md")
    undated_mismatch_r = vr(ev_doc.replace("— 牛客，2025-11-12",
                                          "— 牛客，2025-10-01", 1),
                           e_undated, fn="示例_面试准备.md")
    date_unit = (date_state("未标注")[0] == "unknown"
                 and date_state("2026-99-99")[0] == "invalid"
                 and date_state("2025-11-12")[0] == "known"
                 and not preflight_inputs({"items": [{"date": "未标注"}]}, None)
                 and any("S0" in x for x in
                         preflight_inputs({"items": [{"date": "2026-99-99"}]}, None))
                 and appendix_date_ok("— 牛客，发布日期未标注", "未标注")
                 and not appendix_date_ok("— 牛客，2025-10-01", "未标注"))

    early = json.loads(json.dumps(log))
    early["turns"] = early["turns"][:1]
    early["ended_reason"] = "用户提前结束"
    early4 = json.loads(json.dumps(early))
    early4["turns"][0]["score"] = 4
    early4["turns"][0]["verdict"] = "完整可讲"
    early4_r = vr(build_interactive(early4), lg=early4)                         # I02 应放行
    false_done = json.loads(json.dumps(early))
    false_done["ended_reason"] = "达到目标轮数"
    false_done_r = vr(build_interactive(false_done), lg=false_done)             # I07

    type_r = vr(good_text.replace("面试类型：业务面（一面）", "面试类型：未知类型"))
    fname_r = vr(good_text, fn="随便.md")                                        # D04
    fields_r = vr("\n".join(ln for ln in good_text.splitlines()
                            if not ln.startswith(("**考察意图**", "**答题框架**",
                                                  "**优先级**"))))              # D02

    def blocked(r, code=None):
        if r["passed"]:
            return False
        return any(code in e for e in r["errors"]) if code else True

    v22 = {
        "answer_tamper_blocked": blocked(answer_tamper_r, "S16"),
        "verdict_mismatch_blocked": blocked(verdict_r, "S15"),
        "qref_binding_blocked": blocked(qref_r, "S33"),
        "topic_binding_blocked": blocked(topic_r, "S33"),
        "irrelevant_review_blocked": blocked(irrelevant_r, "S29"),
        "spoof_host_blocked": blocked(host_r, "S10") and blocked(query_r, "S10"),
        "future_date_blocked": blocked(future_r, "S10"),
        "duplicate_url_blocked": blocked(dup_r, "S10"),
        "stray_hf_tag_blocked": blocked(tag_r, "S8"),
        "appendix_drift_blocked": blocked(drift_r, "S9"),
        "missing_window_blocked": blocked(window_r, "S9"),
        "illegal_date_structured": blocked(bad_date_r, "S0"),
        "unknown_date_compatible": date_unit and undated_r["passed"]
                                   and blocked(undated_mismatch_r, "S9"),
        "bad_item_structured": blocked(bad_item_r, "S0"),
        "bad_score_structured": blocked(bad_score_r, "S0"),
        "early_stop_4_accepted": early4_r["passed"],
        "false_completed_blocked": blocked(false_done_r, "S32"),
        "bad_interview_type_blocked": blocked(type_r, "S2"),
        "bad_filename_blocked": blocked(fname_r, "S12"),
        "missing_question_fields_blocked": blocked(fields_r, "S30"),
    }

    ok = (good["passed"] and not bad_r["passed"] and len(bad_r["errors"]) >= 5
          and ev_r["passed"] and any("S10" in e for e in bad_ev_r["errors"])
          and any("S5" in e for e in long_r["errors"])
          and inter_r["passed"] and any("S16" in e for e in no_log_r["errors"])
          and not forged_r["passed"]
          and any("S16" in e for e in unans_r["errors"])
          and any("S18" in e for e in jump_r["errors"])
          and any("S18" in e for e in low_adv_r["errors"])
          and any("S19" in e for e in forged_based_r["errors"])
          and any("S19" in e for e in mislabel_r["errors"])
          and any("S20" in e for e in no_track_r["errors"])
          and bank_ok
          and any("S22" in e for e in no_ref_r["errors"])
          and any("S23" in e for e in no_demo_r["errors"])
          and any("S24" in e for e in no_resume_r["errors"])
          and any("S25" in e for e in no_map_r["errors"])
          and any("S27" in e for e in bad_opt_r["errors"])
          and any("S27" in e for e in opt_only_r["errors"])
          and probe_ok_r["passed"]
          and any("S28" in e for e in p_low_r["errors"])
          and any("S28" in e for e in p_streak_r["errors"])
          and any("S28" in e for e in p_reason_r["errors"])
          and any("S28" in e for e in p_jump_r["errors"])
          and any("S18" in e for e in p_adv_r["errors"])
          and any("S21" in e and "试探性升一档" in e for e in p_lazy_r["errors"])
          and any("S21" in w and "试探性升一档" in w for w in p_lazy_v1_r["warnings"])
          and not any("S21" in e and "试探性升一档" in e
                      for e in p_lazy_v1_r["errors"]))
    ok = ok and all(v22.values()) and bank_ref_ok and gold_ok
    print(json.dumps({
        "self_test": "PASS" if ok else "FAIL",
        "good": {"passed": good["passed"], "errors": good["errors"],
                 "warnings": len(good["warnings"]),
                 "effective_chars": good["info"]["effective_chars"],
                 "question_counts": good["info"]["question_counts"]},
        "bad": {"error_count": len(bad_r["errors"]), "errors": bad_r["errors"]},
        "evidence_ok": {"passed": ev_r["passed"], "errors": ev_r["errors"]},
        "evidence_forged": {"passed": bad_ev_r["passed"], "errors": bad_ev_r["errors"]},
        "body_overlong": {"blocked": not long_r["passed"],
                          "s5_errors": [e for e in long_r["errors"] if e.startswith("S5")][:2]},
        "interactive_ok": {"passed": inter_r["passed"], "errors": inter_r["errors"],
                           "warnings": inter_r["warnings"],
                           "rounds": inter_r["info"].get("interview_rounds")},
        "interactive_no_log_blocked": any("S16" in e for e in no_log_r["errors"]),
        "interactive_forged_blocked": not forged_r["passed"],
        "unanswered_pass_blocked": any("S16" in e for e in unans_r["errors"]),
        "level_jump_blocked": any("S18" in e for e in jump_r["errors"]),
        "low_score_advance_blocked": any("S18" in e for e in low_adv_r["errors"]),
        "forged_followup_blocked": any("S19" in e for e in forged_based_r["errors"]),
        "mislabel_followup_blocked": any("S19" in e for e in mislabel_r["errors"]),
        "missing_trajectory_blocked": any("S20" in e for e in no_track_r["errors"]),
        "v2_bank_ok": {"ok": bank_ok, "version": bank.get("version") if bank else None,
                       "basics_items": len(basics_items)},
        "missing_ref_answer_blocked": any("S22" in e for e in no_ref_r["errors"]),
        "missing_model_answer_blocked": any("S23" in e for e in no_demo_r["errors"]),
        "missing_resume_fix_blocked": any("S24" in e for e in no_resume_r["errors"]),
        "missing_jd_map_blocked": any("S25" in e for e in no_map_r["errors"]),
        "oral_option_blocked": any("S27" in e for e in bad_opt_r["errors"]),
        "option_only_answer_blocked": any("S27" in e for e in opt_only_r["errors"]),
        "good_info": {"resume_status": good["info"].get("resume_status"),
                      "jd_domains": good["info"].get("jd_domains"),
                      "basics_questions": good["info"].get("basics_questions")},
        "max_level": inter_r["info"].get("max_level"),
        "trajectory": [t["level"] for t in log["turns"]],
        "v21_probe_ok": {"passed": probe_ok_r["passed"], "errors": probe_ok_r["errors"],
                         "warnings": probe_ok_r["warnings"],
                         "trajectory": [t["level"] for t in plog["turns"]],
                         "actions": [t["decision"]["action"] for t in plog["turns"]],
                         "max_level": probe_ok_r["info"].get("max_level")},
        "probe_low_score_blocked": any("S28" in e for e in p_low_r["errors"]),
        "probe_without_streak_blocked": any("S28" in e for e in p_streak_r["errors"]),
        "probe_fake_reason_blocked": any("S28" in e for e in p_reason_r["errors"]),
        "probe_jump_blocked": any("S28" in e for e in p_jump_r["errors"]),
        "probe_disguised_advance_blocked": any("S18" in e for e in p_adv_r["errors"]),
        "lazy_no_probe_blocked": any("S21" in e and "试探性升一档" in e
                                     for e in p_lazy_r["errors"]),
        "legacy_log_warn_only": (
            any("S21" in w and "试探性升一档" in w for w in p_lazy_v1_r["warnings"])
            and not any("S21" in e and "试探性升一档" in e
                        for e in p_lazy_v1_r["errors"])),
        "v22": v22,
        "v22_all": all(v22.values()),
        "bank_version": bank.get("version") if bank else None,
        "bank_reference_checked": ref_checked,
        "bank_reference_errors": ref_errs[:6],
        "gold_picks_ok": gold_ok,
        "rules_version": RULES.get("version"),
        "structure_only_note": (
            "校验器只验结构 / 引用 / 状态；答案相关性、经历真实性、评分中肯度"
            "由 references/07 的模型勾选项与人工评测负责"),
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="校验面试准备文档的结构性合规")
    p.add_argument("doc", nargs="?", help="待校验的 .md 文件")
    p.add_argument("--evidence", help="检索证据 evidence.json")
    p.add_argument("--interview-log", help="模拟面试台账 interview-log.json（交互版必填）")
    p.add_argument("--self-test", action="store_true", help="运行内置回归用例")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if not a.doc:
        p.error("需要提供文档路径，或使用 --self-test")
    if not os.path.isfile(a.doc):
        print(json.dumps({"errors": [f"文件不存在：{a.doc}"], "passed": False},
                         ensure_ascii=False, indent=2))
        return 1
    ev = None
    if a.evidence:
        try:
            with open(a.evidence, encoding="utf-8") as f:
                ev = json.load(f)
        except (OSError, ValueError) as e:
            print(json.dumps({"errors": [f"evidence 读取失败：{e}"], "passed": False},
                             ensure_ascii=False, indent=2))
            return 1
    log = None
    if a.interview_log:
        try:
            with open(a.interview_log, encoding="utf-8") as f:
                log = json.load(f)
        except (OSError, ValueError) as e:
            print(json.dumps({"errors": [f"interview-log 读取失败：{e}"], "passed": False},
                             ensure_ascii=False, indent=2))
            return 1
    with open(a.doc, encoding="utf-8") as f:
        text = f.read()
    r = validate(text, ev, a.doc, log)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
