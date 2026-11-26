import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def get_db_path() -> str:
    """自动把 repo.db 放进 Git 公共的 .git 目录，确保所有 worktree 共用同一张锁表"""
    try:
        common_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return str(Path(common_dir) / "repo.db")
    except subprocess.CalledProcessError:
        # 如果不在 Git 仓库中运行，兜底退回到当前目录
        return "repo.db"


def normalize_path(res: str) -> str:
    """统一路径格式：反斜杠和 ./ 全部洗成标准的正斜杠相对路径"""
    cleaned = res.replace("\\", "/")
    return Path(os.path.normpath(cleaned)).as_posix()


def get_conn():
    db_path = get_db_path()
    # isolation_level=None 关闭自动提交，由我们手动用 BEGIN IMMEDIATE 控制全局大锁
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS locks (
            resource TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
    """)
    return conn


def declare_lock(agent_id: str, raw_resources: list[str], ttl: int = 60):
    if not raw_resources:
        print("[错误] 请至少指定一个要锁定的文件路径。")
        return False

    # 先归一化路径并去重
    resources = list(dict.fromkeys(normalize_path(r) for r in raw_resources))

    conn = get_conn()
    now = time.time()
    try:
        # 1. 抢全局大锁 (biglock)
        conn.execute("BEGIN IMMEDIATE")

        # 2. 清理已经超时失联的僵尸锁 (租约到期)
        conn.execute("DELETE FROM locks WHERE expires_at < ?", (now,))

        # 3. 检查要声明的资源里，有没有被其他 Agent 占着的
        placeholders = ",".join("?" for _ in resources)
        conflicts = conn.execute(
            f"SELECT resource, agent_id FROM locks WHERE resource IN ({placeholders}) AND agent_id != ?",
            (*resources, agent_id),
        ).fetchall()

        if conflicts:
            conn.execute("ROLLBACK")
            print(f"[等待] {agent_id} 抢锁失败，以下资源被占用：")
            for res, owner in conflicts:
                print(f"  - {res} (当前持有者: {owner})")
            return False

        # 4. 全部空闲 -> 一次性原子上锁，然后释放 biglock
        for res in resources:
            conn.execute(
                "INSERT OR REPLACE INTO locks (resource, agent_id, expires_at) VALUES (?, ?, ?)",
                (res, agent_id, now + ttl),
            )
        conn.execute("COMMIT")
        print(f"[成功] {agent_id} 已锁定 {resources} (租约 {ttl} 秒)")
        return True
    except Exception as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise e
    finally:
        conn.close()


def unlock(agent_id: str):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM locks WHERE agent_id = ?", (agent_id,))
        conn.execute("COMMIT")
        print(f"[解锁] {agent_id} 已释放 {cur.rowcount} 把锁")
    finally:
        conn.close()


def status():
    db_path = get_db_path()
    conn = get_conn()
    now = time.time()
    rows = conn.execute("SELECT resource, agent_id, expires_at FROM locks").fetchall()
    conn.close()
    print(f"[数据库位置] {db_path}")
    if not rows:
        print("[状态] 当前仓库完全空闲，无锁。")
        return
    print("[状态] 当前活跃锁：")
    for res, owner, exp in rows:
        left = int(exp - now)
        state = f"剩余 {left}s" if left > 0 else "已过期(待清理)"
        print(f"  - {res} -> {owner} ({state})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "lock":
        declare_lock(sys.argv[2], sys.argv[3:])
    elif cmd == "unlock":
        unlock(sys.argv[2])
    else:
        status()