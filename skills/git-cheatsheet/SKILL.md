---
name: git-cheatsheet
description: Git 命令速查。当用户问"怎么撤销提交""怎么回退版本""怎么解决冲突""怎么改最后一次 commit"等 Git 问题时使用。
---

# Git 速查

> 这是学员在第六期亲手写的第一个技能:知识型技能——SKILL.md 本身就是答案的来源。

## 撤销与回退

| 场景 | 命令 | 说明 |
|---|---|---|
| 改乱了文件,还没 add | `git restore <file>` | 丢弃工作区修改,不可恢复 |
| 已经 add,还没 commit | `git restore --staged <file>` | 退出暂存区,修改保留 |
| 撤销最后一次提交,保留修改 | `git reset --soft HEAD~1` | 最常用,改完重新 commit |
| 回退到指定版本,丢弃修改 | `git reset --hard <commit>` | 危险!先 `git log` 确认 |
| 已经 push 了想撤销 | `git revert <commit>` | 生成反向提交,不改历史 |

## 改最后一次 commit

```
git commit --amend          # 追加暂存的修改到上次 commit
git commit --amend -m "新消息"  # 只改提交信息
```

注意:amend 会改变 commit hash,已 push 的提交不要 amend,用 revert。

## 解决冲突

1. `git pull` 冲突后,打开冲突文件,处理 `<<<<<<<` / `=======` / `>>>>>>>` 标记
2. `git add <file>` 标记已解决
3. `git commit` 完成合并

## 找回"丢了"的提交

```
git reflog        # 查看所有 HEAD 移动记录
git reset --hard <hash>   # 回到任意历史位置
```

## 暂存现场

```
git stash          # 临时存起来
git stash pop      # 取回来
```
