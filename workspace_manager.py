import argparse
import subprocess
import sys
from pathlib import Path

import lock_manager


def run_git(
    args: list[str],
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """统一关闭中文路径转义并强制使用 UTF-8 编码"""
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=cwd,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=check,
    )


def get_main_repo_root() -> Path:
    """通过 git worktree list --porcelain 取主工作区根目录（兼容子目录、worktree 与子模块，拒绝 bare 仓库）"""
    res = run_git(["worktree", "list", "--porcelain"], check=False)
    if res.returncode != 0:
        raise RuntimeError("当前目录不在 Git 仓库中，无法定位主工作区。")

    blocks = res.stdout.strip().split("\n\n")
    if not blocks or not blocks[0].strip():
        raise RuntimeError("无法解析 Git worktree 信息。")

    first_lines = blocks[0].splitlines()
    if any(line.strip() == "bare" for line in first_lines):
        raise RuntimeError("不支持在 bare（裸）仓库中使用 workspace_manager。")

    for line in first_lines:
        if line.startswith("worktree "):
            return Path(line[len("worktree "):].strip()).resolve()

    raise RuntimeError("未能从 git worktree list 中解析出主仓库路径。")


def get_worktree_root(repo_root: Path) -> Path:
    return repo_root / ".worktrees"


def get_default_branch(repo_root: Path) -> str:
    """动态识别仓库默认主分支（兼容 main / master 等）"""
    res = run_git(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo_root,
        check=False,
    )
    if res.returncode == 0 and "/" in res.stdout.strip():
        return res.stdout.strip().split("/", 1)[1]

    for candidate in ("main", "master"):
        chk = run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
            cwd=repo_root,
            check=False,
        )
        if chk.returncode == 0:
            return candidate

    cur = run_git(["branch", "--show-current"], cwd=repo_root, check=False)
    return cur.stdout.strip() or "main"


def get_changed_files(wt_path: Path) -> set[str]:
    """使用 status -z 解析隔间内所有改动，完整支持中文、空格与重命名文件"""
    res = run_git(["status", "--porcelain", "-z", "-uall"], cwd=wt_path)
    changed: set[str] = set()
    entries = res.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) < 4:
            i += 1
            continue
        status_code = entry[:2]
        path1 = entry[3:]
        changed.add(lock_manager.normalize_path(path1))
        if "R" in status_code or "C" in status_code:
            i += 1
            if i < len(entries) and entries[i]:
                changed.add(lock_manager.normalize_path(entries[i]))
        i += 1
    return changed


def spawn(agent_id: str, resources: list[str], ttl: int = 300) -> bool:
    """校验 ID -> 检查残留隔间与分支 -> 原子抢锁 -> 开独立 worktree 隔间"""
    if not lock_manager.validate_agent_id(agent_id):
        return False

    repo_root = get_main_repo_root()
    wt_root = get_worktree_root(repo_root)
    wt_path = wt_root / agent_id
    branch_name = f"agent/{agent_id}"

    if wt_path.exists():
        print(f"[错误] {agent_id} 的工作区已存在：{wt_path}，请先 finish 或 abort。")
        return False

    branch_exists = run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_root,
        check=False,
    )
    if branch_exists.returncode == 0:
        print(
            f"[错误] 分支 {branch_name} 已存在（可能是上次合并冲突后保留的提交），"
            "为防止丢失数据已拒绝覆盖。请手动检查或删除该分支后重试。"
        )
        return False

    if not lock_manager.declare_lock(agent_id, resources, ttl=ttl):
        return False

    base_branch = get_default_branch(repo_root)
    try:
        wt_root.mkdir(exist_ok=True)
        run_git(
            ["worktree", "add", "-b", branch_name, str(wt_path), base_branch],
            cwd=repo_root,
        )
        print(
            f"[开房成功] {agent_id} 的独立工作区已就绪：{wt_path} "
            f"(基于 {base_branch}，分支: {branch_name})"
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[错误] 创建工作区失败，正在自动回滚清理：\n{e.stderr.strip()}")
        abort(agent_id)
        return False


def finish(agent_id: str, message: str) -> bool:
    """安检验收 -> 隔间内提交 -> 切回主分支 -> 合并（失败强制 abort） -> 拆隔间并解锁"""
    if not lock_manager.validate_agent_id(agent_id):
        return False

    repo_root = get_main_repo_root()
    wt_path = get_worktree_root(repo_root) / agent_id
    branch_name = f"agent/{agent_id}"

    if not wt_path.exists():
        print(f"[错误] 找不到 {agent_id} 的工作区：{wt_path}")
        return False

    # 1. 核对租约是否有效（拒绝时保留现场）
    locked = lock_manager.get_agent_locks(agent_id)
    if not locked:
        print(
            f"[拦截] {agent_id} 当前没有有效锁（可能已过期），拒绝交货！\n"
            f"  提示：隔间现场已保留，请先重新加锁，或执行 abort {agent_id} 放弃本次改动。"
        )
        return False

    # 2. 扫描隔间内的真实改动
    changed = get_changed_files(wt_path)
    if not changed:
        print(
            f"[提示] {agent_id} 的工作区没有任何改动，无需提交。\n"
            f"  提示：若任务取消，可执行 abort {agent_id} 清理工作区并解锁。"
        )
        return False

    # 3. 越界修改安检（拒绝时保留现场）
    unauthorized = changed - locked
    if unauthorized:
        print(f"[拦截] 检测到越界修改！{agent_id} 修改了未上锁的文件，拒绝合并：")
        for f in sorted(unauthorized):
            print(f"  - {f}")
        print(
            f"  提示：隔间现场已保留。请删除越界文件、调用 lock 补锁，或执行 abort {agent_id} 放弃改动。"
        )
        return False

    # 4. 在隔间内暂存并提交
    try:
        run_git(["add", "-A"], cwd=wt_path)
        diff_chk = run_git(["diff", "--cached", "--quiet"], cwd=wt_path, check=False)
        if diff_chk.returncode == 0:
            print(f"[提示] {agent_id} 暂存区无实际内容变更，取消提交。")
            return False
        run_git(["commit", "-m", message], cwd=wt_path)
    except subprocess.CalledProcessError as e:
        print(f"[错误] 隔间提交失败（现场已保留）：\n{e.stderr.strip()}")
        return False

    # 5. 切回主分支（单独捕获，与 merge 冲突区分开）
    base_branch = get_default_branch(repo_root)
    try:
        run_git(["checkout", base_branch], cwd=repo_root)
    except subprocess.CalledProcessError as e:
        print(
            f"[错误] 无法切换到主分支 {base_branch}（主仓库可能有未提交改动，隔间提交已保留）：\n"
            f"{e.stderr.strip()}"
        )
        return False

    # 6. 合并回主分支（失败时无条件执行 git merge --abort 保全主干）
    try:
        run_git(["merge", "--no-edit", branch_name], cwd=repo_root)
        print(
            f"[交货成功] {agent_id} 的改动 {sorted(changed)} 已安全合并到 {base_branch}！"
        )
    except subprocess.CalledProcessError as e:
        run_git(["merge", "--abort"], cwd=repo_root, check=False)
        print(
            f"[回滚] 合并分支 {branch_name} 失败，已执行 git merge --abort 保全 {base_branch} 分支"
            f"（隔间与提交已保留，请排查冲突）：\n{e.stderr.strip()}"
        )
        return False

    # 7. 合并成功后拆除隔间并解锁
    abort(agent_id)
    return True


def abort(agent_id: str) -> bool:
    """拆除隔间、清理索引、删除临时分支，且通过最外层 finally 保证必定释放锁"""
    if not lock_manager.validate_agent_id(agent_id):
        return False

    ok = True
    try:
        repo_root = get_main_repo_root()
        wt_path = get_worktree_root(repo_root) / agent_id
        branch_name = f"agent/{agent_id}"

        if wt_path.exists():
            res = run_git(
                ["worktree", "remove", "--force", str(wt_path)],
                cwd=repo_root,
                check=False,
            )
            if res.returncode == 0:
                print(f"[清理] 已移除工作区目录：{wt_path}")
            else:
                ok = False
                print(f"[警告] 移除工作区目录失败（可能被其他程序占用）：\n{res.stderr.strip()}")

        run_git(["worktree", "prune"], cwd=repo_root, check=False)

        br_res = run_git(["branch", "-D", branch_name], cwd=repo_root, check=False)
        if br_res.returncode == 0:
            print(f"[清理] 已删除临时分支：{branch_name}")
    finally:
        lock_manager.unlock(agent_id)

    return ok


def list_workspaces() -> bool:
    repo_root = get_main_repo_root()
    res = run_git(["worktree", "list"], cwd=repo_root)
    print("[当前活跃工作区]")
    print(res.stdout.strip())
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent-VCS Git Worktree 隔离工作区管理器")
    sub = parser.add_subparsers(dest="cmd")

    p_spawn = sub.add_parser("spawn", help="原子抢锁并创建独立 worktree 隔间")
    p_spawn.add_argument("agent_id", help="Agent 标识")
    p_spawn.add_argument("resources", nargs="+", help="要锁定的文件路径列表")
    p_spawn.add_argument("--ttl", type=int, default=300, help="租约秒数（默认 300）")

    p_finish = sub.add_parser("finish", help="越界安检、提交并合并回主分支")
    p_finish.add_argument("agent_id", help="Agent 标识")
    p_finish.add_argument("message", nargs="*", help="提交说明")

    p_abort = sub.add_parser("abort", help="强制拆除隔间、删分支并释放锁")
    p_abort.add_argument("agent_id", help="Agent 标识")

    sub.add_parser("list", help="列出当前活跃工作区")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    cmd = args.cmd or "list"

    try:
        if cmd == "spawn":
            ok = spawn(args.agent_id, args.resources, ttl=args.ttl)
        elif cmd == "finish":
            msg = " ".join(args.message) if args.message else f"feat({args.agent_id}): task done"
            ok = finish(args.agent_id, msg)
        elif cmd == "abort":
            ok = abort(args.agent_id)
        else:
            ok = list_workspaces()
        return 0 if ok else 1
    except (RuntimeError, subprocess.CalledProcessError) as e:
        print(f"[错误] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())