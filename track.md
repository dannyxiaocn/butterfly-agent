# Nutshell 开发 Track

> 理念：**Filesystem-As-Everything · Context-native Designing**
>
> 作用：让模型在长任务下的表现更好（持续迭代 + 长流程任务）
> 定位：成为 OpenClaw 要拜访的 sub-agents
> 形态：长任务 · Multi-agent

---

## 用 Nutshell 开发 Nutshell（meta SOP）

> 用 nutshell 的 agent 来完成 nutshell 自身的开发任务——真正的 filesystem-as-everything 实践。

> ⚠️ **核心原则（不可忽略）：实现类任务必须派发给 nutshell_dev 执行，Claude Code 不直接写代码。**
> 唯一例外：维护类操作（更新 track.md / MEMORY.md / 周期检查）可由 Claude Code 直接完成。

### 角色分工

| 角色 | 职责 |
|------|------|
| **Claude Code（我）** | 从 track.md **选取**任务，向 nutshell_dev **派发**任务，**审核**产出，**调整**方向，**标记**完成 + commit ID |
| **nutshell_dev agent** | 接受任务指令，**执行**具体实现（写代码、跑测试、commit），更新自己的 memory/skill/tool |

> **关键原则：任务选取和最终判断权在 Claude Code。** nutshell_dev 是执行者，不是决策者。Claude Code 全程负责任务的正确性与完成质量。

### 工作流（每次迭代）

```
1. Claude Code 读 track.md → 选取合适的 [ ] 任务
2. 【必须】派发给 nutshell_dev：
   nutshell chat --entity nutshell_dev --timeout 300 "任务：<描述>"
   （保存返回的 Session ID）
3. 监控进度
   nutshell log SESSION_ID -n 10      # 查看 agent 对话记录
   nutshell tasks SESSION_ID          # 查看 agent 任务板
4. 审核产出：代码正确性、测试通过、commit 信息
5. 若有问题 → 继续发消息给 nutshell_dev 纠偏
   nutshell chat --session SESSION_ID "修正：..."
6. 通过后 → Claude Code 在 track.md 标记 [x] + commit ID
7. 若发现 nutshell 缺失功能 → Claude Code 在 track.md 加新 [ ] todo
```

### 待完成（nutshell_dev 驱动）
- [x] 让 nutshell_dev 能自主领取 track.md 任务并完成（heartbeat-driven） <!-- 173e884 v1.3.11 -->
- [x] nutshell_dev memory 包含 track.md 当前状态快照 <!-- be6f2cc v1.3.6 -->
    - [x] entity layered memory 目录支持（entity/memory/ → session/core/memory/） <!-- be6f2cc v1.3.6 -->
    - [x] track.md 未完成任务列表动态注入 memory — `--inject-memory track=@track.md` 支持运行时文件注入 <!-- 37a04d2 v1.3.8 -->
- [x] nutshell_dev 能自动在完成后标记 track.md + commit <!-- 31244b1 -->
- [x] [bug] nutshell chat --entity nutshell_dev 默认 timeout 120s 太短，复杂任务会超时但 agent 仍在工作 → 考虑增大默认 timeout 或支持 --timeout 参数透传 <!-- 95329bd v1.3.7 -->

---

## **非常重要的周期性任务**
在每隔5个小时，进行一次检查：
- 当前的系统架构是否清晰
- 系统是否精简，是否存在冗余代码
- logistics文件是否up-to-date（memory、readme、changelog）
- 是否存在巨型文件可以拆分，通过该行为可以受益
- nutshell的功能是否都可以正常运行，是否存在bug
对于以上项，请每次发现上一次的维护时间和当前时间超过5个小时，就请重新彻底检查和维护一次，这是优先级最高的任务，以下为历史检查时间：
**最新检查时间**：19:46 3.25.2026
已完成检查的次数：13


## 全面转向 CLI

agent 自己就可以使用，自己就可以迭代

- [x] 删除 tui，但仍然保留 web 端来监控使用 <!-- 93312c7 v1.3.2 -->
- [x] cli 上提交的chat等等，web上应当能看到完整的、实时的工作，cli启动的和web上启动的应该是一个东西，不应该有差别 <!-- 8606176 v1.3.9 -->
- [x] 全面转向 cli <!-- ee1dc63 v1.3.1 -->

---

## Agent 交互

- [x] git 工作区 <!-- 72d6418 v1.3.16 -->
    - [x] [WIP] 在工作，没有的时候都是想要提交？要让 agent 来进行轮询？ — `git_checkpoint` tool <!-- 72d6418 v1.3.16 -->

---

## 用户交互

- [x] 让任务板用户和 agent 都能看到，让用户能看到 agent 在干嘛、之前干了啥 <!-- 2b907b1 v1.3.3 -->
- [x] `nutshell log [SESSION_ID] [-n N]` — 让用户看到 agent 最近的对话历史（context.jsonl） <!-- 5678b8e v1.3.4 -->

---

## Filesystem-as-everything

- [ ] 如何更好的定义 tool？在我们已经有 bash 的情况下；站在 context（分层 load）的角度去思考一下
    - [x] system prompt 过长要搞定 <!-- 7d45608 v1.1.6 -->
        - [x] [memory recall] 同时，session memory 也要搞 <!-- 5dd735b v1.2.3 -->
            - [x] session memory 里面包含了 project memory，也要分层 <!-- 71f9c66 v1.1.7 -->
    - [x] creator mode skill 要搞，一定要参考 skill-creator 是怎么写的 <!-- d86c2b6 -->
        - [x] [版本控制] 更新不同代 prompt、skills、tools、以及模型的对应版本控制 —> 主要针对于 entity —> 这个是科研迭代系统的重要组件 <!-- d239374 v1.3.14 -->
            - [x] 可以让 Agent 可知的情况下，提交对应的 update request，人类来审核 —> 也就是完成从 session 彻底到 global 的更新（在 nutshell-server 侧给出提示，然后人类通过 cli 工具来同意） <!-- 3abfba4 v1.2.1 -->
            - [x] 把 entity 复制到每个 session 里面，然后做一次同步，这样每个 session 的 agent 只读 session，不更改全局，但它也可以更改，在这个地方要加一个版本控制的机制，然后 nutshell 来协调
    - [x] 权限，安全性是要仔细考虑的问题 <!-- 272c76b v1.3.19 sandbox -->
        - [x] python 可以写出来一些系统级 tool？以及一些为了避免错误的？<!-- 272c76b v1.3.19 sandbox blocked_patterns -->
            - [x] 剩下的都用 bash，可以创建 python，然后 bash 来跑，但这仍然属于 bash
            - [x] 所有的 tool 中系统 python 是 only-系统权限应用，不开放给 agent 来创造/修改的权限，也不需要热插拔
    - [x] 让 agent 可以自己迭代 tool 和 skill（修改）
        - [x] 怎么可以让 agent 自己实现，然后热插拔的可以下一次会话使用（在不用重启系统的情况下，或者是在对话过程中要进行刷新等？tool 对应的就是 executor，executor 的实现要怎么能够满足上述的要求）
            - [x] 现在 bash 是可以在本地实现的
        - [x] agent 怎么修改 skills？是直接修改核心的吗？还是自己可以增加一个版本？那么就是在本地的 session 里面要复制一套，然后在那里面改 —> git 机制
        - [x] agent 需要的一个 playground 也要创建（一个是 local computer，另一个是对应具体的系统文件的位置，这个 Agent's Computer 要怎么设计，类似于机密文件放在什么地方的感觉）
    - [x] 可能要思考一下有 tool 之后的架构要怎么 reshape 了 —> 参考一下 pi-ai? 以及 tinyclaw, ironclaw 这些…

- [x] 站在 skills 的角度思考一下这种分段式 load in 的感觉和思路 <!-- 3c12fce v1.3.10 -->
    - [x] 优化一下系统 prompt，太长了，想一下 skill 的这种"分区"的思路 — memory layer 超过 60 行自动截断，agent 按需 cat <!-- 3c12fce v1.3.10 -->

- [x] [tool]
    - [x] 想让 Agent 自己可以写 tool —> 要怎么设计这个
    - [x] 搜索 —> 这个要搞
        - [x] 还有一个就是不同 web search tool 之间要怎么协调？理论上每个功能的 tool 可以有多个 provider 可以切换
        - [x] brave 的搞完了
    - [x] 实现一个 global tool 和 instance tool（同理 skills）—> 有没有必要？global 是通用的
    - [x] 还是说 CLI 为主，filesystem 为主
    - [x] 实现 tool-call 的线程，让模型可以直接调用 cli，调用工具

- [x] [skills] tool 和 skill 怎么设计？先参考 openclaw 以及 claude code

---

## 兼容

- [x] multiagent <!-- 1871749 v1.2.2 + 679e482 v1.2.4 -->
- [x] cli <!-- ee1dc63 v1.3.1 -->

---

## Multi-agent → 作为核心的 skill

- [ ] 有一个接待 agent，有一个 core agent；一个干活的，一个沟通的
- [ ] 【交互】agent 的新交互模式 —> 像是人来到了 agent 的房间一样，而不是一个在线聊天
    - [ ] 要形成一个看板，就算是 agent 看 agent 也可以直接看看板来得到现在的阶段，等等必要的信息，进一步可以再问接待 agent
- [x] 实现一个 multi-agent 情况的 runtime，把能想到的都搞一遍 <!-- 1871749 v1.2.2 + 679e482 v1.2.4 -->
- [x] agent-agent 之间可以相互对话 communicating 见一个受到保护的过程 —> agent comm protocol，multi-agent 的基础？sub-agent 也是这么回事吗？sub-agent 是临时创建的 <!-- 809efc0 v1.1.9 -->
- [x] 有自己调用 sub-agent 的能力 —> 参考 claude 怎么实现的 <!-- 1871749 v1.2.2 -->

---

## Harness — 给 Agent 反馈，让 Agent 可以自我进化

- [x] 环境与反馈系统 <!-- a26e603 v1.3.18 -->
- [x] 约束系统 — sandbox 等 <!-- 272c76b v1.3.19 -->
- [x] 计算 token 数，让模型根据这个找到更短的路径 *(token 追踪已完成 <!-- 8c4b494 v1.2.7 -->；`nutshell token-report` 可视化 token 经济学)* <!-- 2888655 v1.3.17 -->

---

## UI

- [ ] subagent ACP to OpenClaw
- [x] 做一个 TUI <!-- 4420309 v1.3.12 -->
- [x] 添加一下 CUI，可以让 agent 直接调用使用 <!-- 809efc0 v1.1.9 + ee1dc63 v1.3.1 -->
- [x] 系统兼容 openclaw，直接继承他里面的那些 skills 和 tools? —> 自行 load 就好了
- [x] 接口兼容 claude code，直接在里面用 —> Anthropic SDK

---

## cli-app-for-agent

- [ ] 先实现一个实时通信的，用 skill 来实现
    - [ ] 上线一个动作，获取当前的所有好友（以及在线和离线状态），发送实时信息
    - [ ] 把 tool 变成这种 app —> 更好的交互方式，除了 user prompt 还有 system prompt、system prompt 里面 harness 信息以及各种 app 的通知
- [ ] 看板，协作 notion
- [ ] 虾游戏
- [ ] 给 Agent 做一个 Cli-OS 让它可以在里面随便的玩

---

## Agent Usage

- [x] Repo as a skill (deepwiki) <!-- ffcb72d v1.3.20 -->
- [x] 对于每个 repo，直接创建一个 repo_dev 的 agent 来开发 <!-- 66cfbdf v1.3.21 -->
    - [x] 先建 wiki —> 实现 —> 更新 logistics 文件（readme, changelog, memory）—> git push <!-- 66cfbdf v1.3.21 -->
- [ ] yisebi 创建一个"懂王、行动派"的角色，在各种社交媒体上留言等，提供广泛价值
- [ ] 创建一个游戏高玩，高分速通所有 agent 的游戏

---

## 省 token

- [x] memory 在最前面，一更改整个后面 Cache 都没法用，有什么更好的 memory 的方法和策略能够尽可能多的维持 memory 系统？ <!-- ee7d6eb v1.2.0 -->
- [x] heartbeat 也有这个问题，每次开启后的 heartbeat 会重复大量显示很多 prompt，要不要简写这个，把说明放在一开始？—> 效果 vs 成本 <!-- 0bb3337 v1.2.5 -->
- [x] Tool 作为 filesystem 的主要交互，内容肯定是不能直接丢弃的，但是对于很多程序化生成的，比如看系统状态等等的工具，能否更 fine-grained 只描述差异（维护一个全表格，只增加一个差异来表示呢？）—> 对于高频 tool 更 fine-grained：1. 为了效果；2. 为了成本 <!-- f075de1 v1.3.13 -->
- [x] Prompt 优化 —> 效果 vs 成本 —> 如何更好的规划这个 prompt 的空间，以及计算这样的意义 <!-- 693b3a8 v1.3.15 -->

---

## Cache System

- [x] 省 token <!-- ee7d6eb v1.2.0 -->

---

## Model Selection System

- [x] 是否依赖模型自己的判断更好？—> 自主更改？（比如根据 task 里面描述的难度来判断）<!-- 679e482 v1.2.4 -->
- [ ] 还是说 system-level 也可以建一个快速 evaluate 系统来实现

---

## Dev 交互 — Agent 编辑页面

- [ ] 待设计

---

## Harness（细化）

- [ ] 说话也会让 agent 的表现差很多，什么是 agent 好话呢？
    - [ ] 不要局限在具体的问题；讨论和实现分开干
- [ ] 看一下 skills、harness 大家的实现，学习
- [ ] 看一下 context engineering

