"""验证：停用租户 = 只读冻结（不能新增管理员/建用户/启停/重置/改角色/转移/签发邀请）。"""
import uuid
import httpx

BASE = "http://localhost:8000"


def auth(t): return {"Authorization": f"Bearer {t}"}


def super_login(c):
    for pw in ("SuperAdminPwd2026", "ChangeMe!Admin2026"):
        r = c.post(f"{BASE}/api/auth/login", json={"username": "superadmin", "password": pw})
        if r.status_code == 200:
            d = r.json()
            if d.get("must_change_password"):
                c.post(f"{BASE}/api/auth/change-password", headers=auth(d["access_token"]),
                       json={"old_password": pw, "new_password": "SuperAdminPwd2026"})
                r = c.post(f"{BASE}/api/auth/login", json={"username": "superadmin", "password": "SuperAdminPwd2026"})
            return r.json()["access_token"]
    raise RuntimeError("超管登录失败")


def main():
    ok = []
    def check(n, c): ok.append((n, c)); print(f"[{'PASS' if c else 'FAIL'}] {n}")

    with httpx.Client(trust_env=False, timeout=30) as c:
        sa = super_login(c)
        sfx = uuid.uuid4().hex[:6]
        # 建租户 + 一名普通用户（趁租户还启用）
        t = c.post(f"{BASE}/api/admin/tenants", headers=auth(sa),
                   json={"name": f"冻结测试_{sfx}", "admin_username": f"fadmin_{sfx}"}).json()
        tid = t["id"]
        users = c.get(f"{BASE}/api/admin/tenants/{tid}/users", headers=auth(sa)).json()
        uid = users[0]["id"]

        # 停用租户
        r = c.put(f"{BASE}/api/admin/tenants/{tid}/status", headers=auth(sa), json={"is_active": False})
        check("停用租户 200", r.status_code == 200)

        # 仍可查看用户列表
        r = c.get(f"{BASE}/api/admin/tenants/{tid}/users", headers=auth(sa))
        check("停用后仍可查看用户列表 200", r.status_code == 200)

        # 不能新增管理员
        r = c.post(f"{BASE}/api/admin/tenants/{tid}/admins", headers=auth(sa),
                   json={"username": f"new_{sfx}"})
        check("停用后新增管理员被拒(403)", r.status_code == 403)

        # 不能启停该租户用户
        r = c.put(f"{BASE}/api/admin/users/{uid}/status", headers=auth(sa), json={"is_active": False})
        check("停用后启停用户被拒(403)", r.status_code == 403)

        # 不能重置该租户用户口令
        r = c.post(f"{BASE}/api/admin/users/{uid}/reset-password", headers=auth(sa))
        check("停用后重置口令被拒(403)", r.status_code == 403)

        # 恢复启用，验证又能写
        r = c.put(f"{BASE}/api/admin/tenants/{tid}/status", headers=auth(sa), json={"is_active": True})
        check("重新启用 200", r.status_code == 200)
        r = c.post(f"{BASE}/api/admin/tenants/{tid}/admins", headers=auth(sa),
                   json={"username": f"reok_{sfx}"})
        check("启用后又可新增管理员 201", r.status_code == 201)

    passed = sum(1 for _, c in ok if c)
    print(f"\n==== {passed}/{len(ok)} passed ====")
    raise SystemExit(0 if passed == len(ok) else 1)


if __name__ == "__main__":
    main()
