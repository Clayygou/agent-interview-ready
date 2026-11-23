#!/usr/bin/env python3
"""按难度池 / 题域可复现地随机抽题（仅标准库）。

用法：
  python pick_questions.py --level L1 --group basics --seed demo-seed-001
  python pick_questions.py --level L2 --seed demo-seed-001 --exclude L2-01
  python pick_questions.py --level L1 --group basics --seed demo-seed-001 --tags multimodal
  python pick_questions.py --list --level L1 --group basics

输出 JSON 中的 seed / group / pool / picked.id 必须原样写进 interview-log.json，
validate_doc.py 会 import 本文件的 pick() 用同一算法复算，算出别的题即判
「随机起手不可复现」。校验器与抽题器共用同一份实现，避免两边算法漂移。

本脚本只做抽取，不评判、不生成答案；题库是内部文件，不要把整份内容
塞进提问对话框（泄题）。
"""
import argparse
import json
import os
import random
import sys
from datetime import date

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "question_bank.json")
LEVELS = ("L1", "L2", "L3", "L4")
GROUPS = ("basics", "role", "behavior")  # behavior 供 HR 面起手 / 升档


def load_bank(path=BANK_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pool_of(bank, level, group=None, tags=None):
    """返回按 id 排序的题目列表；排序保证不同机器、不同 Python 版本结果一致。

    group：basics（业务面开局八股热身）/ role（岗位题）/ behavior（HR 面行为题）/ None（整池）。
    tags ：按 domain_tags 取并集过滤，用于 JD 题型映射后的定向抽题。
    """
    if level not in LEVELS:
        raise ValueError(f"未知难度 {level}，可选 {'/'.join(LEVELS)}")
    if group is not None and group not in GROUPS:
        raise ValueError(f"未知题组 {group}，可选 {'/'.join(GROUPS)}")
    items = bank["levels"][level]["items"]
    if group is not None:
        items = [x for x in items if x.get("group") == group]
    if tags:
        wanted = set(tags)
        items = [x for x in items if wanted & set(x.get("domain_tags", []))]
    return sorted(items, key=lambda x: x["id"])


def pick(seed, level, exclude=None, bank=None, bank_path=BANK_PATH,
         group=None, tags=None):
    """确定性抽取：同 seed + 同难度池 + 同题组 + 同排除集 → 同结果。"""
    bank = bank if bank is not None else load_bank(bank_path)
    items = [x for x in pool_of(bank, level, group, tags)
             if x["id"] not in set(exclude or [])]
    if not items:
        raise ValueError(f"{level} 池（group={group}, tags={tags}）在排除后为空")
    ids = [x["id"] for x in items]
    chosen = random.Random(str(seed)).choice(ids)  # 对 id 列表抽样，避免 dict 顺序影响
    return {
        "seed": str(seed),
        "level": level,
        "group": group,
        "tags": sorted(set(tags or [])),
        "pool": ids,
        "picked": next(x for x in items if x["id"] == chosen),
        "excluded": sorted(set(exclude or [])),
        "bank_version": bank.get("version", "unknown"),
    }


def main():
    p = argparse.ArgumentParser(description="从面试题库按难度 / 题组可复现地随机抽题")
    p.add_argument("--level", choices=LEVELS, default="L1")
    p.add_argument("--group", choices=GROUPS, help="题组：basics 八股热身 / role 岗位题 / behavior HR 行为题")
    p.add_argument("--tags", help="按 domain_tags 过滤，逗号分隔，取并集")
    p.add_argument("--seed", help="随机种子；省略则用当天日期，输出里会回带 seed")
    p.add_argument("--exclude", action="append", default=[], help="排除的题目 id，可重复")
    p.add_argument("--bank", default=BANK_PATH, help="题库路径")
    p.add_argument("--list", action="store_true", help="只列出该池的 id / topic / 题域")
    a = p.parse_args()
    try:
        bank = load_bank(a.bank)
        tags = [x.strip() for x in (a.tags or "").split(",") if x.strip()]
        if a.list:
            print(json.dumps([{"id": x["id"], "topic": x["topic"],
                               "group": x.get("group", "role"),
                               "domain_tags": x.get("domain_tags", [])}
                              for x in pool_of(bank, a.level, a.group, tags)],
                             ensure_ascii=False, indent=2))
            return 0
        seed = a.seed or date.today().strftime("%Y%m%d")
        print(json.dumps(pick(seed, a.level, a.exclude, bank, a.bank,
                              a.group, tags), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
