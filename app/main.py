import asyncio
from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer
from sanic import Request, Sanic, json, redirect
from sanic.response import file

from app.config import Config
from app.db import (
    GateError,
    claim_verify,
    finish_verify,
    get_db,
    get_flow,
    init_db,
    list_flows,
    policy_view,
    run_extract,
    run_land,
    set_policy,
    update_nodes,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ser = URLSafeSerializer(Config.SECRET, salt="cc-flow-session")

app = Sanic("cc-flow-lab")
app.static("/static", str(STATIC), name="static")


def _user(request: Request) -> str | None:
    raw = request.cookies.get("flow_session")
    if not raw:
        return None
    try:
        data = ser.loads(raw)
        u = data.get("user") if isinstance(data, dict) else None
        return u if u == Config.ADMIN_USER else None
    except BadSignature:
        return None


def _unauth():
    return json({"detail": "未登录"}, status=401)


def _gate_error(err: GateError):
    return json(err.payload(), status=err.status)


@app.listener("before_server_start")
async def _boot(_app, _loop):
    init_db()


@app.get("/")
async def root(request: Request):
    return redirect("/app/" if _user(request) else "/login")


@app.get("/login")
async def login_page(request: Request):
    return await file(STATIC / "login.html")


@app.get("/app/", name="spa_root")
async def spa_root(request: Request):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.get("/app/<path:path>", name="spa_path")
async def spa_path(request: Request, path: str = ""):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = request.json or {}
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json({"ok": False, "error": "账号或密码错误"}, status=400)
    resp = json({"ok": True, "user": user})
    resp.add_cookie(
        "flow_session",
        ser.dumps({"user": user}),
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    resp = json({"ok": True})
    resp.delete_cookie("flow_session", path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    user = _user(request)
    if not user:
        return _unauth()
    return json({"user": user, "lab": Config.LAB_NAME, "domain": "数据工程"})


@app.get("/api/policy")
async def api_policy(request: Request):
    if not _user(request):
        return _unauth()
    with get_db() as conn:
        return json({"policy": policy_view(conn)})


@app.post("/api/policy")
async def api_set_policy(request: Request):
    if not _user(request):
        return _unauth()
    body = request.json or {}
    try:
        with get_db() as conn:
            policy = set_policy(conn, body.get("key"))
    except GateError as err:
        return _gate_error(err)
    return json({"policy": policy})


@app.get("/api/flows")
async def api_flows(request: Request):
    if not _user(request):
        return _unauth()
    with get_db() as conn:
        return json({"items": list_flows(conn), "policy": policy_view(conn)})


@app.get("/api/flows/<flow_id:int>")
async def api_flow(request: Request, flow_id: int):
    if not _user(request):
        return _unauth()
    with get_db() as conn:
        item = get_flow(conn, flow_id)
        policy = policy_view(conn)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json({"item": item, "policy": policy})


@app.patch("/api/flows/<flow_id:int>/nodes")
async def api_nodes(request: Request, flow_id: int):
    if not _user(request):
        return _unauth()
    body = request.json or {}
    try:
        with get_db() as conn:
            item = update_nodes(conn, flow_id, body)
            policy = policy_view(conn)
    except GateError as err:
        return _gate_error(err)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json({"item": item, "policy": policy})


@app.post("/api/flows/<flow_id:int>/extract")
async def api_extract(request: Request, flow_id: int):
    if not _user(request):
        return _unauth()
    try:
        with get_db() as conn:
            item = run_extract(conn, flow_id)
    except GateError as err:
        return _gate_error(err)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


@app.post("/api/flows/<flow_id:int>/verify")
async def api_verify(request: Request, flow_id: int):
    """节点齐套核验：双方争抢时仅先手那次过。

    默认同步完成核验；`hold_ms` 用于模拟“核验还在跑”的窗口，
    该窗口内策略不得变更，其它请求会记为抢检落败。
    """
    if not _user(request):
        return _unauth()
    body = request.json or {}
    try:
        with get_db() as conn:
            claimed, policy = claim_verify(conn, flow_id)
    except GateError as err:
        return _gate_error(err)
    if not claimed:
        return json({"detail": "流不存在"}, status=404)

    hold_ms = body.get("hold_ms")
    if isinstance(hold_ms, (int, float)) and 0 < hold_ms <= 30000:
        async def _later():
            await asyncio.sleep(hold_ms / 1000)
            with get_db() as conn:
                finish_verify(conn, flow_id, policy["key"])

        asyncio.create_task(_later())
        return json(
            {
                "item": claimed,
                "policy": policy,
                "verifying": True,
                "detail": "核验进行中",
            }
        )

    with get_db() as conn:
        item = finish_verify(conn, flow_id, policy["key"])
        policy = policy_view(conn)
    return json({"item": item, "policy": policy})


@app.post("/api/flows/<flow_id:int>/land")
async def api_land(request: Request, flow_id: int):
    if not _user(request):
        return _unauth()
    body = request.json or {}
    try:
        with get_db() as conn:
            item = run_land(conn, flow_id, path=body.get("path"))
            policy = policy_view(conn)
    except GateError as err:
        return _gate_error(err)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json({"item": item, "policy": policy})
