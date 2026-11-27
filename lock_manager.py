import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_agent_id(agent_id: str) -> bool:
    """白名单校验 agent_id，防止路径穿越和非法分支名"""
    if not AGENT_ID_PATTERN.fullmatch(agent_id):
        print(
            f"[错误] 非法的 agent_id: '{agent_id}'。"
            "仅允许字母/数字开头，后续包含字母、数字、下划线或短横线（最长 64 字符）。"
        )
        return False
    return True


def get_db_path() -> str:
    """强制使用绝对路径定位 Git 公共目录下的 repo.db，不在仓库内则直接报错"""
    try:
        common_dir = subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
        ).strip()
        return str(Path(common_dir) / "repo.db")
    except subprocess.CalledProcessError as e:
        raise RuntimeError("当前目录不在 Git 仓库中，无法定位 .git/repo.db。") from e


def normalize_path(res: str) -> str:
    """统一路径格式：洗成正斜杠相对路径；Windows 下统一小写防止大小写绕过"""
    cleaned = res.replace("\\", "/")
    norm = Path(os.path.normpath(cleaned)).as_posix()
    if os.name == "nt":
        norm = norm.lower()
    return norm


def ensure_schema(conn: sqlite3.Connection):
    """按需开启 WAL 并建表，避免每次连接都触发无谓的写事务"""
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() != "wal":
        conn.execute("PRAGMA journal_mode = WAL")

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='locks'"
    ).fetchone()
    if exists is not None:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS locks (
                resource TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def get_conn() -> sqlite3.Connection:
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 5000")
    ensure_schema(conn)
    return conn


def declare_lock(agent_id: str, raw_resources: list[str], ttl: int = 60) -> bool:
    if not validate_agent_id(agent_id):
        return False
    if not raw_resources:
        print("[错误] 请至少指定一个要锁定的文件路径。")
        return False
    if ttl <= 0:
        print("[错误] 租约时间 ttl 必须大于 0。")
        return False

    resources = list(dict.fromkeys(normalize_path(r) for r in raw_resources))

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()

        # 清理已过期的僵尸锁
        conn.execute("DELETE FROM locks WHERE expires_at < ?", (now,))

        # 检查是否与其他 Agent 冲突
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

        for res in resources:
            conn.execute(
                "INSERT OR REPLACE INTO locks (resource, agent_id, expires_at) VALUES (?, ?, ?)",
                (res, agent_id, now + ttl),
            )
        conn.execute("COMMIT")
        print(f"[成功] {agent_id} 已锁定 {resources} (租约 {ttl} 秒)")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def renew(agent_id: str, ttl: int = 60) -> bool:
    """为指定 Agent 当前仍有效的锁续期"""
    if not validate_agent_id(agent_id):
        return False
    if ttl <= 0:
        print("[错误] 续期时间 ttl 必须大于 0。")
        return False

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()

        conn.execute("DELETE FROM locks WHERE expires_at < ?", (now,))
        cur = conn.execute(
            "UPDATE locks SET expires_at = ? WHERE agent_id = ?",
            (now + ttl, agent_id),
        )
        conn.execute("COMMIT")

        if cur.rowcount == 0:
            print(f"[失败] {agent_id} 当前没有可续期的有效锁（可能已过期或未加锁）。")
            return False

        print(f"[续期] {agent_id} 的 {cur.rowcount} 把锁已续期 {ttl} 秒")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def unlock(agent_id: str) -> bool:
    if not validate_agent_id(agent_id):
        return False

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM locks WHERE agent_id = ?", (agent_id,))
        conn.execute("COMMIT")
        print(f"[解锁] {agent_id} 已释放 {cur.rowcount} 把锁")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_agent_locks(agent_id: str) -> set[str]:
    """查询某个 Agent 当前仍处于有效租约内的已锁文件集合"""
    if not validate_agent_id(agent_id):
        return set()

    conn = get_conn()
    try:
        now = time.time()
        rows = conn.execute(
            "SELECT resource FROM locks WHERE agent_id = ? AND expires_at >= ?",
            (agent_id, now),
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def status() -> bool:
    db_path = get_db_path()
    conn = get_conn()
    try:
        now = time.time()
        rows = conn.execute(
            "SELECT resource, agent_id, expires_at FROM locks"
        ).fetchall()
    finally:
        conn.close()

    print(f"[数据库位置] {db_path}")
    if not rows:
        print("[状态] 当前仓库完全空闲，无锁。")
        return True

    print("[状态] 当前活跃锁：")
    for res, owner, exp in rows:
        left = int(exp - now)
        state = f"剩余 {left}s" if left > 0 else "已过期(待清理)"
        print(f"  - {res} -> {owner} ({state})")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent-VCS SQLite 资源锁管理器")
    sub = parser.add_subparsers(dest="cmd")

    p_lock = sub.add_parser("lock", help="原子抢锁")
    p_lock.add_argument("agent_id", help="Agent 标识")
    p_lock.add_argument("resources", nargs="+", help="要锁定的文件路径列表")
    p_lock.add_argument("--ttl", type=int, default=60, help="租约秒数（默认 60）")

    p_renew = sub.add_parser("renew", help="为已有有效锁续期")
    p_renew.add_argument("agent_id", help="Agent 标识")
    p_renew.add_argument("--ttl", type=int, default=60, help="续期秒数（默认 60）")

    p_unlock = sub.add_parser("unlock", help="释放指定 Agent 的全部锁")
    p_unlock.add_argument("agent_id", help="Agent 标识")

    sub.add_parser("status", help="查看当前锁状态")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    cmd = args.cmd or "status"

    try:
        if cmd == "lock":
            ok = declare_lock(args.agent_id, args.resources, ttl=args.ttl)
        elif cmd == "renew":
            ok = renew(args.agent_id, ttl=args.ttl)
        elif cmd == "unlock":
            ok = unlock(args.agent_id)
        else:
            ok = status()
        return 0 if ok else 1
    except RuntimeError as e:
        print(f"[错误] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())