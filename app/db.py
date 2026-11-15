import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from app.config import Config

# ---- 节点齐套闸的状态常量（服务端唯一裁定口径） -------------------------
GATE_PENDING = "待检"
GATE_RUNNING = "核验中"
GATE_PASSED = "已过"
GATE_REVIEW = "待复核"
GATE_RETURNED = "已退回"

# 齐套策略：本日钉死其中一种，不许换弦。
POLICY_NAMES = {
    "block": "缺项拦截",
    "review": "缺项待复核",
}
DEFAULT_POLICY_KEY = "review"
POLICY_OPTIONS = [{"key": k, "name": v} for k, v in POLICY_NAMES.items()]

# 必填节点字段（中文名即缺项卡片展示名）。编码节点空缺也算缺项。
NODE_FIELDS = (
    ("title", "标题节点", "title"),
    ("code", "编码节点", "code"),
    ("source", "来源节点", "source_uri"),
)

SEEDS = [
    ("目录扫描流", "scan_flow", "待抽取", "https://mock.local/scan"),
    ("列表翻页流", "page_flow", "待抽取", "https://mock.local/list"),
    ("附件下载流", "file_flow", "待抽取", "file://drops/pack"),
    ("变更监听流", "watch_flow", "待抽取", "https://mock.local/watch"),
    # 编码节点空缺的样例：用于暴露缺项卡片（空编码也算缺项）。
    ("编码补录流", "", "待抽取", "https://mock.local/code"),
]

_FLOW_COLS = (
    "id, title, code, status, source_uri, extracted, landed, "
    "gate_state, gate_missing, gate_checked_at, verify_lost, verify_lost_at, "
    "read_only, land_path"
)

# 注意：code 不设 UNIQUE —— 编码节点空缺是合法状态，由齐套闸裁定成缺项。
FLOWS_DDL = '''
    CREATE TABLE {name} (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      code TEXT NOT NULL,
      status TEXT NOT NULL,
      source_uri TEXT NOT NULL,
      extracted INTEGER NOT NULL DEFAULT 0,
      landed INTEGER NOT NULL DEFAULT 0,
      gate_state TEXT NOT NULL DEFAULT '待检',
      gate_missing TEXT NOT NULL DEFAULT '[]',
      gate_checked_at TEXT,
      verify_lost INTEGER NOT NULL DEFAULT 0,
      verify_lost_at TEXT,
      read_only INTEGER NOT NULL DEFAULT 0,
      land_path TEXT
    )
'''


class GateError(Exception):
    """齐套闸/落库门槛错误；payload 直接回给前端。"""

    def __init__(self, detail, status=409, reason="gate", **extra):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.reason = reason
        self.extra = extra

    def payload(self):
        data = {"ok": False, "detail": self.detail, "reason": self.reason}
        data.update(self.extra)
        return data


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_str():
    return date.today().isoformat()


def _policy_key_default():
    key = (os.environ.get("CC_FLOW_POLICY") or DEFAULT_POLICY_KEY).strip()
    return key if key in POLICY_NAMES else DEFAULT_POLICY_KEY


def connect(db_path=None):
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn


@contextmanager
def get_db(db_path=None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_columns(conn, table, columns):
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _has_unique_code(conn):
    for idx in conn.execute("PRAGMA index_list(flows)").fetchall():
        if not idx["unique"]:
            continue
        cols = [
            r["name"]
            for r in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
        ]
        if cols == ["code"]:
            return True
    return False


def _migrate_drop_code_unique(conn):
    """旧库把 code 设为 UNIQUE；空编码缺项会互相撞键，需重建表去掉约束。"""
    if not _has_unique_code(conn):
        return
    conn.executescript(
        """
        ALTER TABLE flows RENAME TO flows_old;
        """
        + FLOWS_DDL.format(name="flows")
        + """;
        INSERT INTO flows (
          id, title, code, status, source_uri, extracted, landed,
          gate_state, gate_missing, gate_checked_at, verify_lost,
          verify_lost_at, read_only, land_path
        )
        SELECT
          id, title, code, status, source_uri, extracted, landed,
          gate_state, gate_missing, gate_checked_at, verify_lost,
          verify_lost_at, read_only, land_path
        FROM flows_old;
        DROP TABLE flows_old;
        """
    )


def init_db(db_path=None):
    with get_db(db_path) as conn:
        conn.executescript(FLOWS_DDL.format(name="IF NOT EXISTS flows"))
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS policy_day (
              day TEXT PRIMARY KEY,
              policy_key TEXT NOT NULL,
              set_at TEXT NOT NULL
            );
            """
        )
        _ensure_columns(
            conn,
            "flows",
            {
                "gate_state": "TEXT NOT NULL DEFAULT '待检'",
                "gate_missing": "TEXT NOT NULL DEFAULT '[]'",
                "gate_checked_at": "TEXT",
                "verify_lost": "INTEGER NOT NULL DEFAULT 0",
                "verify_lost_at": "TEXT",
                "read_only": "INTEGER NOT NULL DEFAULT 0",
                "land_path": "TEXT",
            },
        )
        _migrate_drop_code_unique(conn)
        if conn.execute("SELECT COUNT(*) c FROM flows").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO flows (title, code, status, source_uri) VALUES (?,?,?,?)",
                SEEDS,
            )


# ---- 策略：本日钉死一种，写完当天不许换弦 -------------------------------
def _policy_dict(row):
    key = row["policy_key"]
    return {
        "day": row["day"],
        "key": key,
        "name": POLICY_NAMES.get(key, key),
        "set_at": row["set_at"],
        "pinned": True,
        "editable": False,
        "options": POLICY_OPTIONS,
    }


def _policy_view(conn, day=None):
    """今日策略视图：已写过就是钉死记录，未写过给出预置值（可改）。"""
    day = day or today_str()
    row = conn.execute(
        "SELECT day, policy_key, set_at FROM policy_day WHERE day=?", (day,)
    ).fetchone()
    if row is not None:
        return _policy_dict(row)
    key = _policy_key_default()
    return {
        "day": day,
        "key": key,
        "name": POLICY_NAMES[key],
        "set_at": None,
        "pinned": False,
        "editable": True,
        "options": POLICY_OPTIONS,
    }


def policy_view(conn, day=None):
    """只读查看今日策略（未写过时不落库，仅给出预置值）。"""
    return _policy_view(conn, day)


def ensure_policy(conn, day=None):
    """把今日策略钉死（首次写入即“写完”），之后当天不许换弦。

    首次核验/落库时自动按预置值钉死；也可由用户先显式选一种。
    """
    day = day or today_str()
    row = conn.execute(
        "SELECT day, policy_key, set_at FROM policy_day WHERE day=?", (day,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO policy_day (day, policy_key, set_at) VALUES (?,?,?)",
            (day, _policy_key_default(), now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT day, policy_key, set_at FROM policy_day WHERE day=?", (day,)
        ).fetchone()
    return _policy_dict(row)


def verifying_count(conn):
    return conn.execute(
        "SELECT COUNT(*) c FROM flows WHERE gate_state=?", (GATE_RUNNING,)
    ).fetchone()["c"]


def set_policy(conn, key):
    key = (key or "").strip()
    if key not in POLICY_NAMES:
        raise GateError(
            "齐套策略取值无效，只能是 block 或 review",
            status=400,
            reason="invalid_policy",
            policy=_policy_view(conn),
        )
    current = _policy_view(conn)
    if verifying_count(conn):
        # 核验还在跑时不准改齐套策略（也包含首次钉死）。
        raise GateError(
            "核验还在跑时不准改齐套策略",
            reason="verifying",
            policy=current,
        )
    if current["pinned"]:
        if key == current["key"]:
            return current
        raise GateError(
            "齐套策略本日已钉死，写完当天不许换弦",
            reason="pinned",
            policy=current,
        )
    conn.execute(
        "INSERT INTO policy_day (day, policy_key, set_at) VALUES (?,?,?)",
        (current["day"], key, now_iso()),
    )
    conn.commit()
    return ensure_policy(conn)


# ---- 节点齐套：由服务端裁定 ---------------------------------------------
def missing_nodes(item):
    values = {
        "title": item.get("title"),
        "code": item.get("code"),
        "source": item.get("source_uri"),
    }
    missing = []
    for key, label, field in NODE_FIELDS:
        value = values.get(key)
        if value is None or not str(value).strip():
            missing.append({"key": key, "label": label, "field": field})
    return missing


def _load_missing(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if isinstance(entry, dict):
            out.append(
                {
                    "key": entry.get("key", ""),
                    "label": entry.get("label", ""),
                    "field": entry.get("field", ""),
                }
            )
        elif isinstance(entry, str):
            out.append({"key": entry, "label": entry, "field": ""})
    return out


def _item(row):
    data = dict(row)
    stored = _load_missing(data.pop("gate_missing", "[]"))
    # 缺项按当前节点字段实时裁定（服务端口径），缺项卡片随时可见。
    live = missing_nodes(data)
    data["missing"] = live
    data["missing_nodes"] = [m["label"] for m in live]
    data["missing_node_keys"] = [m["key"] for m in live]
    # 核验当时的缺项快照，留作审计。
    data["missing_at_check"] = [m["label"] for m in stored]
    data["gate_label"] = data.get("gate_state")
    data["verify_lost"] = int(data.get("verify_lost") or 0)
    data["read_only"] = bool(data.get("read_only"))
    return data


def list_flows(conn):
    return [
        _item(r)
        for r in conn.execute(f"SELECT {_FLOW_COLS} FROM flows ORDER BY id")
    ]


def get_flow(conn, flow_id: int):
    row = conn.execute(
        f"SELECT {_FLOW_COLS} FROM flows WHERE id=?", (flow_id,)
    ).fetchone()
    return _item(row) if row else None


def update_nodes(conn, flow_id: int, body):
    """维护节点字段；编码清空属于缺项，不被拒绝，由齐套闸裁定。"""
    item = get_flow(conn, flow_id)
    if not item:
        return None
    if item["read_only"]:
        raise GateError("列表只读：待复核流不能改节点", reason="readonly")
    if item["gate_state"] == GATE_RUNNING:
        raise GateError("核验还在跑，不能改节点", reason="verifying")
    if item["status"] == "已落库":
        raise GateError("已落库的流不能改节点", reason="landed")

    fields = {}
    for key, column in (("title", "title"), ("code", "code"), ("source_uri", "source_uri"), ("source", "source_uri")):
        if key in body:
            value = body.get(key)
            fields[column] = "" if value is None else str(value).strip()
    if not fields:
        raise GateError("没有可更新的节点字段", status=400, reason="invalid_nodes")

    sets = ", ".join(f"{col}=?" for col in fields)
    # 节点一改，齐套结论作废，回到待检由服务端重新裁定。
    conn.execute(
        f"UPDATE flows SET {sets}, gate_state=?, gate_missing='[]', "
        "gate_checked_at=NULL, verify_lost=0, verify_lost_at=NULL WHERE id=?",
        (*fields.values(), GATE_PENDING, flow_id),
    )
    return get_flow(conn, flow_id)


# ---- 抽取 / 核验 / 落库 -------------------------------------------------
def run_extract(conn, flow_id: int):
    item = get_flow(conn, flow_id)
    if not item:
        return None
    if item["read_only"] or item["gate_state"] == GATE_REVIEW:
        raise GateError("列表只读：待复核流不能重抽", reason="readonly")
    if item["gate_state"] == GATE_RUNNING:
        raise GateError("核验还在跑，不能重抽", reason="verifying")
    if item["status"] in ("待抽取", "抽取失败"):
        n = 15 + (flow_id * 4) % 10
        conn.execute(
            "UPDATE flows SET status=?, extracted=?, landed=0, gate_state=?, "
            "gate_missing='[]', gate_checked_at=NULL, verify_lost=0, "
            "verify_lost_at=NULL, read_only=0, land_path=NULL WHERE id=?",
            ("已抽取待落库", n, GATE_PENDING, flow_id),
        )
    return get_flow(conn, flow_id)


def claim_verify(conn, flow_id: int):
    """抢检：只有先手（待检 -> 核验中 的 CAS 成功者）才能核验。

    抢到后立刻把本日策略钉死：本次核验就按这条路径走，当天不许换弦。
    """
    item = get_flow(conn, flow_id)
    if not item:
        return None, None
    if item["read_only"]:
        raise GateError("列表只读：待复核流不能重复核验", reason="readonly")
    if item["gate_state"] == GATE_RETURNED:
        raise GateError(
            "该流已被缺项拦截退回，请重新抽取后再核验", reason="returned"
        )
    if item["gate_state"] == GATE_RUNNING:
        # 先手核验还在跑，后来者直接落败，不重复计数到已完成分支里。
        conn.execute(
            "UPDATE flows SET verify_lost=verify_lost+1, verify_lost_at=? WHERE id=?",
            (now_iso(), flow_id),
        )
        conn.commit()
        raise GateError(
            "抢检落败：先手核验仍在进行，仅先手那次过",
            reason="lost",
            lost=True,
            gate_state=GATE_RUNNING,
        )
    if item["status"] != "已抽取待落库":
        raise GateError("未抽取不能核验", reason="not_extracted")
    cur = conn.execute(
        "UPDATE flows SET gate_state=? WHERE id=? AND gate_state=? AND read_only=0",
        (GATE_RUNNING, flow_id, GATE_PENDING),
    )
    if cur.rowcount != 1:
        conn.execute(
            "UPDATE flows SET verify_lost=verify_lost+1, verify_lost_at=? WHERE id=?",
            (now_iso(), flow_id),
        )
        conn.commit()
        latest = get_flow(conn, flow_id)
        raise GateError(
            "抢检落败：该流已由先手完成核验，仅先手那次过",
            reason="lost",
            lost=True,
            gate_state=latest["gate_state"] if latest else None,
        )
    # 立刻提交，让“核验中”对其它请求可见：核验还在跑时不准改策略。
    conn.commit()
    policy = ensure_policy(conn)
    return get_flow(conn, flow_id), policy


def finish_verify(conn, flow_id: int, policy_key: str):
    item = get_flow(conn, flow_id)
    if not item:
        return None
    if item["gate_state"] != GATE_RUNNING:
        return item
    missing = missing_nodes(item)
    raw_missing = json.dumps(missing, ensure_ascii=False)
    if missing:
        if policy_key == "block":
            # 缺项 -> 整批拦住落库并退回重抽。
            conn.execute(
                "UPDATE flows SET gate_state=?, gate_missing=?, gate_checked_at=?, "
                "status=?, extracted=0, landed=0, read_only=0 WHERE id=?",
                (GATE_RETURNED, raw_missing, now_iso(), "待抽取", flow_id),
            )
        else:
            # 缺项 -> 仍可落库，但改成待复核且列表只读。
            conn.execute(
                "UPDATE flows SET gate_state=?, gate_missing=?, gate_checked_at=? "
                "WHERE id=?",
                (GATE_REVIEW, raw_missing, now_iso(), flow_id),
            )
    else:
        conn.execute(
            "UPDATE flows SET gate_state=?, gate_missing='[]', gate_checked_at=? "
            "WHERE id=?",
            (GATE_PASSED, now_iso(), flow_id),
        )
    conn.commit()
    return get_flow(conn, flow_id)


def run_land(conn, flow_id: int, path=None):
    item = get_flow(conn, flow_id)
    if not item:
        return None
    if item["read_only"]:
        raise GateError("列表只读：该流待复核，不能再次落库", reason="readonly")

    gate = item["gate_state"]
    if gate == GATE_PENDING:
        raise GateError(
            "未核验不能落库，请先完成节点齐套核验", reason="unverified"
        )
    if gate == GATE_RUNNING:
        raise GateError("核验还在跑，暂不能落库", reason="verifying")

    policy = ensure_policy(conn)
    provided = str(path).strip() if path is not None else ""

    # 页面点落库时带上它认为生效的齐套路径；与钉死策略对不上就点亮提示。
    if provided and provided not in POLICY_NAMES:
        raise GateError(
            "节点齐套路径偏了：页面路径无法识别",
            reason="path_mismatch",
            path_mismatch=True,
            actual_path=provided,
            expected_path=policy["key"],
            policy=policy,
        )
    if provided and provided != policy["key"]:
        raise GateError(
            "节点齐套路径偏了：页面路径与当前齐套策略对不上",
            reason="path_mismatch",
            path_mismatch=True,
            actual_path=provided,
            expected_path=policy["key"],
            policy=policy,
        )

    missing = item["missing_nodes"]
    if missing:
        expected = GATE_RETURNED if policy["key"] == "block" else GATE_REVIEW
        if gate != expected:
            # 实际路径（已记录的闸门走向）与齐套策略对不上。
            raise GateError(
                "节点齐套路径偏了：实际路径与齐套策略对不上",
                reason="path_mismatch",
                path_mismatch=True,
                actual_path=gate,
                expected_path=expected,
                policy=policy,
            )
        if policy["key"] == "block":
            raise GateError(
                "缺项拦截：整批不予落库，已退回重抽",
                reason="blocked",
                missing_nodes=missing,
            )
    elif gate != GATE_PASSED:
        raise GateError("节点齐套未过闸，不能落库", reason="gate")

    if item["status"] != "已抽取待落库":
        raise GateError("当前状态不可落库", reason="status")

    read_only = 1 if gate == GATE_REVIEW else 0
    conn.execute(
        "UPDATE flows SET status=?, landed=?, read_only=?, land_path=? WHERE id=?",
        ("已落库", item["extracted"], read_only, policy["key"], flow_id),
    )
    return get_flow(conn, flow_id)
