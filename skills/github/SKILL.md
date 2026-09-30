---
name: github
description: GitHub 仓库速查技能:仓库指标怎么看(star/fork/issue)、如何向 GitHub 仓库提问、配合 MCP repo 服务器查仓库的最佳姿势。
---

# GitHub 仓库速查

> 第 11 期引入,配合 MCP repo 服务器使用:Alex 先加载本技能,再用 mcp_call_tool 查仓库,把技能里的"解读框架"套用到查询结果上。

## 仓库核心指标怎么读

| 指标 | 含义 | 怎么判断好坏 |
|---|---|---|
| Star | 别人给这个仓库点的"赞" | 数量级比绝对值重要:1k+ 可用、10k+ 热门、50k+ 现象级 |
| Fork | 别人复制了一份去改 | Fork/Star 比值高 → 说明很多人基于它做二次开发 |
| Open Issues | 还没关掉的 issue | 数量大不一定是坏事,看维护者是否活跃回复 |
| 主语言 | 仓库主要技术栈 | 判断能否自己参与贡献、读源码 |

## 查仓库的三步提问法(配合 MCP)

1. **先搜索**:`search_repositories("deepseek")` 看全景,对比 star 数找热门仓库
2. **再详情**:`get_repository_info(owner, repo)` 盯一个仓库看细节(简介/语言/issue 数)
3. **后观察**:`list_issues(owner, repo)` 看社区在讨论什么——issue 标题往往暴露真实使用痛点

## 汇报模板(把工具结果讲给人听)

- 一句话定位:这是谁、做什么的
- 关键数字:star / fork / issue 各多少,和同类仓库比处于什么档位
- 社区信号:最近 issue 里大家在关心什么
- 建议动作:值不值得用/跟进/贡献

## 常见坑

- 不要只报数字不解读:"85000 star"不如"85k star,属于现象级开源项目"
- owner/repo 拼错是最常见错误,工具会明确报"仓库不存在"
- 教学模拟数据只覆盖 deepseek-ai 与 openai 的部分仓库;生产中把 repo 服务器换成 GitHub 官方 MCP server,提问姿势完全一样
