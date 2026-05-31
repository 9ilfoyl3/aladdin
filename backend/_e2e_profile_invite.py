"""验证：个人资料(简介/头像)自助维护、租户资料(超管)、邀请链接创建用户追踪。"""
import uuid
import httpx

BASE = "http://localhost:8000"
# 1x1 png data url（合法、极小）
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


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
    def check(n, c, e=""): ok.append((n, c)); print(f"[{'PASS' if c else 'FAIL'}] {n}" + (f" -- {e}" if e else ""))

    with httpx.Client(trust_env=False, timeout=30) as c:
        sa = super_login(c)
        sfx = uuid.uuid4().hex[:6]

        # 超管 me/profile：身份=超级管理员
        prof = c.get(f"{BASE}/api/auth/me/profile", headers=auth(sa)).json()
        check("超管资料 is_super_admin", prof.get("is_super_admin") is True, str(prof.get("role_names")))
        check("超管身份名=超级管理员", prof.get("role_names") == ["超级管理员"])

        # 建租户（带简介+头像）
        r = c.post(f"{BASE}/api/admin/tenants", headers=auth(sa),
                   json={"name": f"企业_{sfx}", "admin_username": f"adm_{sfx}",
                         "description": "一家测试企业", "avatar": PNG})
        check("建租户带简介/头像 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
        t = r.json()
        tid = t["id"]
        check("租户返回简介", t.get("description") == "一家测试企业")
        check("租户返回头像", bool(t.get("avatar")))
        atemp = t["admin_temp_password"]

        # 超管改租户资料
        r = c.put(f"{BASE}/api/admin/tenants/{tid}/profile", headers=auth(sa),
                  json={"description": "改过的简介"})
        check("超管改租户资料 200", r.status_code == 200 and r.json().get("description") == "改过的简介",
              f"{r.status_code} {r.text[:120]}")

        # 列表含简介/头像
        lst = c.get(f"{BASE}/api/admin/tenants", headers=auth(sa)).json()
        row = next((x for x in lst if x["id"] == tid), None)
        check("租户列表含简介/头像字段", bool(row) and "description" in row and "avatar" in row)

        # 租管登录改密
        d = c.post(f"{BASE}/api/auth/login", json={"username": f"adm_{sfx}", "password": atemp}).json()
        c.post(f"{BASE}/api/auth/change-password", headers=auth(d["access_token"]),
               json={"old_password": atemp, "new_password": "AdmPwd123"})
        adm = c.post(f"{BASE}/api/auth/login", json={"username": f"adm_{sfx}", "password": "AdmPwd123"}).json()["access_token"]

        # 租管自助维护资料
        r = c.put(f"{BASE}/api/auth/me/profile", headers=auth(adm),
                  json={"description": "我是管理员", "avatar": PNG})
        check("租管自助改资料 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        check("租管资料回显简介", r.json().get("description") == "我是管理员")
        check("租管资料回显头像", bool(r.json().get("avatar")))
        check("租管身份名含 admin（前端经 roleLabel 显示为管理员）", "admin" in r.json().get("role_names", []), str(r.json().get("role_names")))

        # 租管不能改租户资料（平台级）——应 403
        r = c.put(f"{BASE}/api/admin/tenants/{tid}/profile", headers=auth(adm),
                  json={"description": "越权改"})
        check("租管改租户资料被拒(403)", r.status_code == 403, f"{r.status_code}")

        # 头像非法（非 data:image）应 400
        r = c.put(f"{BASE}/api/auth/me/profile", headers=auth(adm), json={"avatar": "http://x/y.png"})
        check("非法头像被拒(400)", r.status_code == 400, f"{r.status_code}")

        # 租管发"建用户"邀请，查创建用户（先空）
        inv = c.post(f"{BASE}/api/admin/invitations", headers=auth(adm),
                     json={"scope": "create_user", "expires_in_hours": 168}).json()
        token = inv["token"]
        invid = inv["id"]
        users0 = c.get(f"{BASE}/api/admin/invitations/{invid}/users", headers=auth(adm)).json()
        check("邀请初始无创建用户", users0 == [], str(users0))

        # 用该邀请接受建号
        uname = f"inv_user_{sfx}"
        r = c.post(f"{BASE}/api/invitations/{token}/accept", json={"username": uname, "password": "UserPwd123"})
        check("接受邀请建号 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

        # 再查创建用户
        users1 = c.get(f"{BASE}/api/admin/invitations/{invid}/users", headers=auth(adm)).json()
        check("邀请创建用户可查到", any(u["username"] == uname for u in users1), str([u["username"] for u in users1]))

    passed = sum(1 for _, c in ok if c)
    print(f"\n==== {passed}/{len(ok)} passed ====")
    raise SystemExit(0 if passed == len(ok) else 1)


if __name__ == "__main__":
    main()
