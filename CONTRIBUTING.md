# 协作指南

仓库：<https://github.com/Kk111777/HuaweiCup2026A>。`Kk111777` 与 `Zysishuiyears` 均可推送任务分支、提交 PR、审核和合并。队友必须先接受私有仓库邀请。

## 1. 首次加入

登录自己的 GitHub 账号，接受仓库邀请，再克隆：

```powershell
git clone https://github.com/Kk111777/HuaweiCup2026A.git
cd HuaweiCup2026A
```

在此项目内设置自己的提交身份，勿照抄另一位成员的名字和邮箱：

```powershell
git config --local user.name "你的GitHub用户名"
git config --local user.email "你的GitHub已验证邮箱或隐私邮箱"
```

隐私邮箱可在 GitHub 账号的邮件设置页查看。Git 提交身份与推送登录是两回事；推送时需使用已被邀请的 GitHub 账号。Python 环境见 [环境说明](docs/environment.md)。

## 2. 为新任务创建分支

先检查工作区；如果有未提交的改动，先保留或按需要提交，再切换分支，勿直接覆盖：

```powershell
git status
git switch main
git pull --ff-only origin main
```

例如，队友开始数据预处理任务：

```powershell
git switch -c feat/Zysishuiyears/data-preprocessing
```

所有者可使用 `feat/Kk111777/model-baseline`；修复任务使用 `fix/<用户名>/<任务>`。一项任务一个短期分支，不使用个人长期分支堆积互不相关的修改。

若 `pull --ff-only` 失败，先检查分叉原因；不要强制覆盖本地或远端历史。

## 3. 修改与验证

- Python 程序放在 `src/`，分析 Notebook 放在 `notebooks/`，实际测试放在 `tests/`。
- 新增算法应说明输入、运行命令、参数、输出以及验证结果；涉及随机过程时记录随机种子。
- 添加第三方依赖时，同时更新依赖清单和环境说明。
- 提交前检查 Notebook 输出，避免包含凭据、私人信息或无关的大量结果。
- `.gitignore` 只防止新文件被跟踪；已跟踪的敏感文件需要单独处理，不能仅靠新增忽略规则。

## 4. 需要分享时提交、上传

先查看自己修改了什么：

```powershell
git status --short
git diff
git diff --check
git fetch origin
git log --oneline --left-right HEAD...origin/main
```

如果 `main` 有需要集成的新提交，在工作区干净、自己的工作已保存后，将 `origin/main` 合并到当前任务分支，再运行适用的验证。不要自动把 `main` 强制推到远端，也不要覆盖同一任务分支上队友的新提交。

仅暂存本次任务相关的文件，例如实际创建了下列程序时：

```powershell
git add -- src/preprocess.py
git diff --cached
git diff --cached --check
git commit -m "feat: 添加数据预处理程序"
git push -u origin HEAD
```

示例文件名需替换为实际修改的路径。若同一任务分支已有其他成员推送的新提交，推送被拒绝时，先获取并整合这些提交，再验证和推送；不使用强推绕过。

在 Codex 中，日常修改只留本地；说“提交”只创建本地提交，说“同步上传”或“上传”才执行上传并创建或更新 PR。单说“同步”时需要明确拉取还是上传。代码保存到磁盘不等于已经提交，提交也不等于上传成功。

## 5. 创建、审核和合并 PR

1. 打开仓库的 **Pull requests**，点击 **New pull request**。
2. `base` 选择 `main`，`compare` 选择自己的任务分支。
3. 按模板填写具体改动、运行或检查结果，以及已知限制；未完成的工作可使用 Draft PR。
4. 请另一位成员审核。审核者查看 **Files changed**，必要时运行程序，再批准或提出修改意见。
5. 修改继续提交并推送到同一分支，PR 会随之更新；无需为同一任务重复创建 PR。
6. 约定在另一人审核后，由任一成员使用 **Squash and merge** 合并。合并后 GitHub 自动删除远端任务分支（以仓库设置及实际状态为准）。

这是免费私有仓库下的团队约定，平台不强制阻止直接推送 `main` 或未经审核的合并。在 Codex 中，创建 PR 的授权不包含合并；只有明确要求合并才执行。

## 6. 合并后更新本地

确认工作区干净，然后执行：

```powershell
git switch main
git pull --ff-only origin main
git fetch --prune origin
```

下一项任务从最新的 `main` 创建新分支。Squash 合并后本地旧分支可能不会被 Git 判定为已合并；保留它不会影响继续开发。需要清理时先确认 PR 已合并、内容已纳入 `main`，不要盲目强制删除。

## 7. 冲突和协作中断

发生冲突时先查看 `git status`，共同确认冲突文件的正确内容，再编辑、测试并完成合并。不要靠整文件选择“全部使用我的”覆盖另一人的工作。无法确定时保留当前状态并沟通。

若未准备好处理刚开始的合并，可在确认本次合并前工作区干净的前提下使用 `git merge --abort`。不要用 `reset --hard` 清理不明状态。

仓库不可见时先确认：使用了自己的受邀账号、邀请已接受、Git 推送凭据对应此账号。插件可访问仓库不代表本机 Git 已经登录；两者需分别验证。
