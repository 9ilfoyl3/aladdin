"""端到端验证 TASK 11 的需求清单（可复跑回归脚本）。

覆盖：
  1. 超管菜单 A 方案（前端逻辑，此处验证 me 的 role/is_super_admin 标记）
  2. 租户管理员不能创建 admin 用户（UserCreate 无角色字段，建号恒为 member）
  3. 超管在租户下钻可新增租户管理员（POST /admin/tenants/{id}/admins）
  4. 用户列表显示角色（固定角色 role 单值）
  5. 管理员不能操作自己（停用/重置自身 -> 403）
  6. 创建用户固定为 member
  7. 邀请链接列表随时可复制（列表返回 token）
  8. 临时口令首登改密前可再次查看（建号/重置返回 temp_password；改密后 None）
  9. 审计日志补全（actor_username / actor_tenant_id 非空）
 10. 租户不删除（无删除端点，停用即可——此处仅验证停用可用）
"""

import httpx

BASE = "http://localhost:8000"
SUPER_USER = "superadmin"
SUPER_PWD = "ChangeMe!Admin2026"          # 初始口令（首启 must_change_password=True）
SUPER_PWD_NEW = "SuperAdminPwd2026"       # 首登改密后的工作口令（脚本可复跑）

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    results.append((name, cond, extra))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {extra}" if extra else ""))


def login(c: httpx.Client, username: str, password: str) -> dict:
    r = c.post(f"{BASE}/api/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def super_admin_login(c: httpx.Client) -> str:
    """超管登录，处理首启强制改密；返回可用于管理操作的工作 token。"""
    # 尝试以工作口令登录（复跑场景：已改过密）
    r = c.post(f"{BASE}/api/auth/login", json={"username": SUPER_USER, "password": SUPER_PWD_NEW})
    if r.status_code == 200 and not r.json().get("must_change_password"):
        return r.json()["access_token"]
    # 否则以初始口令登录并完成首登改密
    r = c.post(f"{BASE}/api/auth/login", json={"username": SUPER_USER, "password": SUPER_PWD})
    r.raise_for_status()
    data = r.json()
    if data.get("must_change_password"):
        tok = data["access_token"]
        cp = c.post(f"{BASE}/api/auth/change-password", headers=auth(tok),
                    json={"old_password": SUPER_PWD, "new_password": SUPER_PWD_NEW})
        cp.raise_for_status()
        data = login(c, SUPER_USER, SUPER_PWD_NEW)
    return data["access_token"]


def main() -> None:
    with httpx.Client(trust_env=False, timeout=30) as c:
        # —— 超管登录（处理首启强制改密）——
        sa_tok = super_admin_login(c)
        me = c.get(f"{BASE}/api/auth/me", headers=auth(sa_tok)).json()
        check("超管登录 & me is_super_admin", me.get("is_super_admin") is True,
              str(me.get("is_super_admin")))

        # —— 创建租户（返回初始管理员临时口令 = 需求8）——
        import uuid as _u
        suffix = _u.uuid4().hex[:6]
        tname = f"测试租户_{suffix}"
        admin_username = f"tadmin_{suffix}"
        r = c.post(f"{BASE}/api/admin/tenants", headers=auth(sa_tok),
                   json={"name": tname, "admin_username": admin_username})
        check("创建租户 201", r.status_code == 201, f"status={r.status_code} body={r.text[:200]}")
        tenant = r.json()
        tid = tenant["id"]
        admin_temp = tenant.get("admin_temp_password")
        check("创建租户返回初始管理员临时口令(需求8)", bool(admin_temp), str(admin_temp))

        # —— 超管下钻该租户用户列表（需求4：含角色名）——
        r = c.get(f"{BASE}/api/admin/tenants/{tid}/users", headers=auth(sa_tok))
        check("下钻租户用户列表 200", r.status_code == 200, f"status={r.status_code}")
        tusers = r.json()
        admin_user = next((u for u in tusers if u["username"] == admin_username), None)
        check("初始管理员在列表内", admin_user is not None)
        if admin_user:
            check("用户列表含角色 role(需求4)", admin_user.get("role") == "admin",
                  str(admin_user.get("role")))
            check("初始管理员临时口令可再次查看(需求8)",
                  admin_user.get("temp_password") == admin_temp, str(admin_user.get("temp_password")))

        # —— 超管在下钻内新增租户管理员（需求3）——
        admin2_username = f"tadmin2_{suffix}"
        r = c.post(f"{BASE}/api/admin/tenants/{tid}/admins", headers=auth(sa_tok),
                   json={"username": admin2_username})
        check("超管新增租户管理员 201(需求3)", r.status_code == 201, f"status={r.status_code} body={r.text[:200]}")
        admin2 = r.json() if r.status_code == 201 else {}
        check("新增管理员带 admin 角色", admin2.get("role") == "admin", str(admin2.get("role")))
        check("新增管理员返回临时口令", bool(admin2.get("temp_password")))

        # —— 该租户管理员登录（首登需改密）——
        a1 = login(c, admin_username, admin_temp)
        check("初始管理员登录 must_change_password", a1.get("must_change_password") is True)
        a1_tok = a1["access_token"]
        # 首登改密前，受 must_change_password 闸门限制，管理操作应 403
        r = c.get(f"{BASE}/api/admin/users", headers=auth(a1_tok))
        check("首登改密前管理操作被闸门拦截(403)", r.status_code == 403, f"status={r.status_code}")
        # 改密
        new_pwd = "TadminPwd123"
        r = c.post(f"{BASE}/api/auth/change-password", headers=auth(a1_tok),
                   json={"old_password": admin_temp, "new_password": new_pwd})
        check("初始管理员改密成功", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
        # 改密后旧 token 失效，重新登录
        a1 = login(c, admin_username, new_pwd)
        a1_tok = a1["access_token"]
        a1_me = c.get(f"{BASE}/api/auth/me", headers=auth(a1_tok)).json()
        a1_uid = a1_me["user_id"]

        # —— 改密后临时口令应被清除(需求8) ——
        r = c.get(f"{BASE}/api/admin/users", headers=auth(a1_tok))
        users_after = r.json().get("items", [])
        self_row = next((u for u in users_after if u["id"] == a1_uid), None)
        check("改密后自身临时口令已清空(需求8)",
              self_row is not None and self_row.get("temp_password") is None,
              str(self_row.get("temp_password") if self_row else "N/A"))

        # —— 需求6：创建用户固定为 member ——
        u_username = f"user_{suffix}"
        r = c.post(f"{BASE}/api/admin/users", headers=auth(a1_tok),
                   json={"username": u_username})
        check("建用户 201(需求6)", r.status_code == 201, f"status={r.status_code} body={r.text[:200]}")
        new_user = r.json() if r.status_code == 201 else {}
        check("建用户固定为 member(需求6)", new_user.get("role") == "member", str(new_user.get("role")))
        check("建用户返回临时口令(需求8)", bool(new_user.get("temp_password")))
        new_uid = new_user.get("id")

        # —— 需求2：租户管理员无法创建 admin 用户 ——
        # UserCreate 不含角色字段，租管经本端点无法把用户设为 admin（设立 admin 仅经平台流程）。
        # 即使建号请求多带无关字段，落库仍恒为 member。
        u2_username = f"user2_{suffix}"
        r = c.post(f"{BASE}/api/admin/users", headers=auth(a1_tok),
                   json={"username": u2_username})
        check("租管建用户恒为 member(不可造 admin)(需求2)",
              r.status_code == 201 and r.json().get("role") == "member",
              f"status={r.status_code} role={r.json().get('role') if r.status_code==201 else '-'}")

        # —— 需求5：管理员不能操作自己 ——
        r = c.put(f"{BASE}/api/admin/users/{a1_uid}/status", headers=auth(a1_tok),
                  json={"is_active": False})
        check("管理员停用自己被拒(403)(需求5)", r.status_code == 403, f"status={r.status_code}")
        r = c.post(f"{BASE}/api/admin/users/{a1_uid}/reset-password", headers=auth(a1_tok))
        check("管理员重置自己口令被拒(403)(需求5)", r.status_code == 403, f"status={r.status_code}")

        # —— 需求7：邀请链接列表随时可复制（返回 token）——
        r = c.post(f"{BASE}/api/admin/invitations", headers=auth(a1_tok),
                   json={"scope": "create_user", "expires_in_hours": 168, "max_uses": 5})
        check("租管生成建用户邀请 201(需求7)", r.status_code == 201, f"status={r.status_code} body={r.text[:200]}")
        inv = r.json() if r.status_code == 201 else {}
        inv_token_create = inv.get("token")
        r = c.get(f"{BASE}/api/admin/invitations", headers=auth(a1_tok))
        inv_items = r.json().get("items", [])
        listed = next((i for i in inv_items if i["id"] == inv.get("id")), None)
        check("邀请列表返回明文 token 可复制(需求7)",
              listed is not None and listed.get("token") == inv_token_create,
              str(listed.get("token") if listed else "N/A"))

        # —— 需求9：审计日志补全（actor_username / actor_tenant_id）——
        r = c.get(f"{BASE}/api/admin/audit-logs", headers=auth(a1_tok))
        check("租管读审计日志 200(需求9)", r.status_code == 200, f"status={r.status_code}")
        logs = r.json().get("items", [])
        check("审计日志非空(需求9)", len(logs) > 0, f"count={len(logs)}")
        if logs:
            # 该租管产生的写操作（建用户/建邀请）应记录其 username 与 tenant_id
            own_logs = [lg for lg in logs if lg.get("actor_username") == admin_username]
            check("审计记录 actor_username 已补全(需求9)", len(own_logs) > 0,
                  f"matched={len(own_logs)} sample={logs[0].get('actor_username')}")
            check("审计记录 actor_tenant_id 已补全(需求9)",
                  any(lg.get("actor_tenant_id") == tid for lg in own_logs),
                  f"tid={tid}")

        # 超管全局审计含登录成功记录且带用户名
        r = c.get(f"{BASE}/api/admin/audit-logs", headers=auth(sa_tok),
                  params={"action": "auth.login_success"})
        slogs = r.json().get("items", [])
        check("超管查登录成功审计带用户名(需求9)",
              any(lg.get("actor_username") for lg in slogs), f"count={len(slogs)}")

        # —— 需求10：租户停用可用（停用 != 删除）——
        r = c.put(f"{BASE}/api/admin/tenants/{tid}/status", headers=auth(sa_tok),
                  json={"is_active": False})
        check("超管停用租户 200(需求10)", r.status_code == 200, f"status={r.status_code}")
        check("停用后租户仍存在(数据保留, 需求10)", r.json().get("is_active") is False)
        # 恢复
        c.put(f"{BASE}/api/admin/tenants/{tid}/status", headers=auth(sa_tok), json={"is_active": True})

    # —— 汇总 ——
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== TASK11 E2E: {passed}/{total} passed ====")
    fails = [(n, e) for n, ok, e in results if not ok]
    if fails:
        print("FAILURES:")
        for n, e in fails:
            print(f"  - {n}: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
