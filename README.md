# agent-vcs

面向多 AI Agent 并发协作的本地 Git 协调工具：基于 **SQLite 原子锁**（`.git/repo.db`）与 **Git Worktree 隔离工作区**，防止多个 Agent 同时修改同一文件引发合并冲突，并在交付前拦截越界修改。

> 环境要求：Python 3.10+、Git（零第三方依赖，兼容 Windows / Linux / macOS）

---

## 核心工作流

### 1. 抢锁并创建独立隔间（`spawn`）
为 Agent 申请文件锁并创建独立的 `.worktrees/<agent_id>` 目录和临时分支 `agent/<agent_id>`（默认租约 300 秒）：

```bash
python workspace_manager.py spawn agent_A src/weapon.py src/ammo.py --ttl 300
```

### 2. 在独立隔间内修改代码
所有文件改动都在 `.worktrees/agent_A/` 中进行，互不干扰其他 Agent 或主工作区。

### 3. 安检、提交并合并回主分支（`finish`）
自动核对锁是否有效、检查是否修改了未上锁文件；全部通过后提交并合并回默认主分支，随后自动清理隔间与锁：

```bash
python workspace_manager.py finish agent_A "feat: implement weapon fire logic"
```

> 若被拦截（锁过期、越界修改、合并冲突），工具会**保留隔间现场与分支**，不会删除已写代码。

### 4. 取消任务或清理现场（`abort`）
放弃本次改动，强制移除工作区目录、删除临时分支并释放锁：

```bash
python workspace_manager.py abort agent_A
```

---

## 常用辅助命令

```bash
# 查看当前活跃工作区
python workspace_manager.py list

# 查看当前锁状态与剩余租约时间
python lock_manager.py status

# 长任务续期（默认续期 60 秒，可自定义 --ttl）
python lock_manager.py renew agent_A --ttl 300

# 中途为同一 Agent 补锁新文件（需无人占用）
python lock_manager.py lock agent_A src/helper.py --ttl 300

# 手动释放锁
python lock_manager.py unlock agent_A
```

---

## 使用注意事项（避坑清单）

1. **资源路径约定**：推荐使用相对仓库根目录的路径（如 `src/weapon.py`）。传入仓库内的绝对路径会被自动归一化为仓库相对路径；指向仓库外的路径会被直接拒绝。
2. **文件级锁粒度**：锁按具体文件路径精确匹配，**锁定目录名不会递归授权该目录下的子文件**。
3. **长任务必须续期**：`spawn` 默认租约 300 秒，`lock` 默认 60 秒。租约过期后锁会被自动清理，其他 Agent 可抢占同一文件，长任务请定期执行 `lock_manager.py renew <agent_id> --ttl <秒数>`。
4. **保留 `.worktrees/` 忽略规则**：请务必在 `.gitignore` 中保留 `.worktrees/`，防止隔离工作区被主仓库误追踪。
5. **网络盘 / 云同步目录**：工具会自动检测 UNC 路径及 OneDrive / Dropbox / 坚果云等同步目录并关闭 SQLite WAL 模式；也可通过环境变量 `AGENT_VCS_WAL=0` 手动关闭。