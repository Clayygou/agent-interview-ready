# agent-interview-ready

面向 AI Agent 岗位的**单场面试准备 Skill**：拆 JD、分析简历、检索证据、生成定制题库，并在交付前用对话框逐题模拟面试，把回答、评判与示范作答写进同一份文档。

设计取向是**可校验**：分层加载控制上下文成本，`rules.json` 统一规则，脚本对结构、引用与状态转移做确定性检查。校验通过只代表结构一致，不代表答案质量或经历真实性。

## 特性

- **分层加载**：L1 元数据常驻，L2 `SKILL.md` 只做路由，L3 `references/*.md` 按步骤加载，脚本只执行不读入上下文。
- **真实交互**：逐题弹对话框采集文本回答，固定四选项，按 0~4 分 rubric 评判，追问必须引用上一轮回答原句。
- **自适应难度**：首题从八股池（业务面）或行为池（HR 面）按种子随机抽；常规升档由 4 分触发，同档连续追问 2 次仍 2~3 分可走合规 `probe` 换考点升一档。
- **JD 映射**：JD 指纹 → 题域配比 → 可校验的章 3 映射表；上传真实简历时章 4 给「位置 → 问题 → 改法 → 对应 JD」修改清单。
- **确定性门禁**：`validate_doc.py` 覆盖 S0~S34 检查，退出码 0 才交付。题库抽题与校验共用同一份 `pick()` 实现。
- **事实边界**：检索无有效来源即写「未检索到有效面经」，不打高频标签；教学示例与候选人真实经历分开标注。

## 安装

把整个目录放到 WorkBuddy 用户级 skills 目录，目录名须与 `SKILL.md` 的 `name` 一致：

```text
~/.workbuddy/skills/agent-interview-ready/
```

会话会自动发现该目录并加载路由层。技能市场的「已安装技能」列表只统计市场安装的技能，手动放置的 Skill 不出现在该列表，属正常现象。

## 使用

四项必填：**公司名称、岗位 JD、个人简历、本场面试类型**（业务面 / HR 面）。可选：案例 / 作品集。

```text
帮我准备 XX 公司的 AI Agent 工程师业务面，JD 和简历已发，面试类型是专业一面。
```

四项齐全后直接开工，不再追问补充信息。缺失时弹一次对话框问清并停止。

- **默认交互版**：生成过程中逐题弹对话框模拟面试，回答、评判与示范作答写入章 8。
- **紧凑版**：明确说「不要提问 / 直接出文档」，跳过交互、不生成章 8；参考答案与简历修改清单仍保留。

对话框固定四选项：`这题我不会` / `需要提示` / `直接作答` / `换一道题`。除「这题我不会」外，只选选项无文本一律重问同题，不记轮次。

流程：校验输入 → 拆 JD → 检索面经 → 公司调研 → Gap 分析 → 题型映射 → 生成章 1~7 → 交互面试 → 并入章 8 → 脚本校验 → 交付单个 `.md`。

## 目录结构

```text
agent-interview-ready/
├── SKILL.md                        路由层（常驻 L2）
├── README.md                       本文件，不进上下文
├── references/                     按需加载 L3
│   ├── 01-input-contract.md        输入契约、归一化、开局对话框
│   ├── 02-workflow.md              10 步流程、evidence / log 契约
│   ├── 03-output-spec.md           章 0~8 规格、字数预算、字段格式
│   ├── 04-agent-domain.md          Agent 岗位考点索引与事实边界
│   ├── 05-degradation.md           降级表（仅失败时读）
│   ├── 06-style-and-truth.md       排版、可访问性、事实标注
│   ├── 07-checklist.md             自检清单：模型勾 / 脚本验
│   ├── 08-interactive-interview.md 对话框、评分 rubric（仅交互版）
│   ├── 09-question-bank.md         难度阶梯、抽题、自适应决策表（仅交互版）
│   ├── 10-jd-profile.md            JD 指纹 → 题域 → 题量映射
│   └── 11-evaluation.md            验收规格与诚实清单
├── scripts/
│   ├── rules.json                  单一规则源：题量 / 评分 / 状态转移 / 结束状态
│   ├── validate_doc.py             确定性校验器（仅标准库，S0~S34）
│   ├── question_bank.json          题库 34 题，带知识库题号与评分锚点
│   └── pick_questions.py           按难度 / 题组 / 题域可复现抽题
└── examples/
    ├── sample-section.md           输出格式节选
    ├── evidence.example.json       有检索证据的台账样例
    ├── evidence.empty.json         无检索工具的台账样例
    ├── interview-log.example.json  6 轮台账样例
    └── interview-log.probe.example.json  probe 升档轨迹样例
```

## 校验

```bash
python scripts/validate_doc.py "XX公司_AI Agent工程师_面试准备.md"
python scripts/validate_doc.py "XX公司_AI Agent工程师_面试准备.md" --evidence evidence.json --interview-log interview-log.json
python scripts/validate_doc.py --self-test

python scripts/pick_questions.py --level L1 --group basics --seed your-seed
python scripts/pick_questions.py --list --level L1 --group basics
```

退出码 `0` = 无 error，可交付；`1` = 必须修复重跑。交互版必须传 `--interview-log`。

脚本检查章节、题量、字数、引用编号、来源主机、附录一致性、回答全文逐字比对、评分映射、状态转移、首题可复算与 probe 合规。脚本**不判断内容好坏**：参考答案是否正确、JD 映射语义是否相符、经历是否真实，由 `07-checklist.md` 的语义项与用户本人确认。

## 可选依赖

`04-agent-domain.md` 是**索引，不是题库正文**：按题号引用外部 AI Agent 面试知识库的 `INTERVIEW.md` 与 `code/*.py`，按需读取命中行，不整篇复制。`scripts/validate_doc.py` 在若干相对位置探测该知识库；路径不存在时按 `05-degradation.md` 降级——跳过领域题库，只用 JD 与岗位通用能力命题，并在章 0 标注「领域题库未接入」。

## 已知限制

- 校验通过只代表 `structure_status`，不代表答案相关性、评分中肯度或经历真实性。
- `evidence.json` 与 `interview-log.json` 由模型按工具结果、对话框结果转录，脚本只能验一致性，不能证明工具真的调用过或用户真的点过选项。彻底防伪造需要工具侧直写文件。
- `--self-test` 的结构夹具把同一段参考答案复用于不同题目，不是答案质量金标准。
- 评分 rubric、难度档位、JD 映射语义仍是模型判断。
- 修复重跑设停止条件：连续两次无改善就保留诊断并交付未通过的草稿，不靠补写事实过检。
- 英文产物、跨模型稳定性、首次提问耗时未测；英文降级为「只翻译正文」。

## License

MIT
