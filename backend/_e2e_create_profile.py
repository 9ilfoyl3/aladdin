"""验证：创建租户/用户时即可设置头像与简介。"""
import uuid, httpx
BASE = "http://localhost:8000"
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
    ok=[]
    def check(n,c,e=""): ok.append((n,c)); print(f"[{'PASS' if c else 'FAIL'}] {n}"+(f" -- {e}" if e else ""))
    with httpx.Client(trust_env=False, timeout=30) as c:
        sa = super_login(c)
        sfx = uuid.uuid4().hex[:6]
        # 建租户带头像+简介
        t = c.post(f"{BASE}/api/admin/tenants", headers=auth(sa),
                   json={"name": f"企业_{sfx}", "admin_username": f"adm_{sfx}",
                         "description": "创建即设置的简介", "avatar": PNG}).json()
        check("租户创建即带简介", t.get("description") == "创建即设置的简介")
        check("租户创建即带头像", bool(t.get("avatar")))
        tid = t["id"]; atemp = t["admin_temp_password"]
        d = c.post(f"{BASE}/api/auth/login", json={"username": f"adm_{sfx}", "password": atemp}).json()
        c.post(f"{BASE}/api/auth/change-password", headers=auth(d["access_token"]),
               json={"old_password": atemp, "new_password": "AdmPwd123"})
        adm = c.post(f"{BASE}/api/auth/login", json={"username": f"adm_{sfx}", "password": "AdmPwd123"}).json()["access_token"]
        # 建用户带头像+简介
        u = c.post(f"{BASE}/api/admin/users", headers=auth(adm),
                   json={"username": f"u_{sfx}", "role_names": [], "description": "用户创建简介", "avatar": PNG}).json()
        check("用户创建即带简介", u.get("description") == "用户创建简介", str(u.get("description")))
        check("用户创建即带头像", bool(u.get("avatar")))
        # 列表里能看到头像/简介
        lst = c.get(f"{BASE}/api/admin/users?page=1&page_size=50", headers=auth(adm)).json()
        row = next((x for x in lst["items"] if x["username"] == f"u_{sfx}"), None)
        check("用户列表回显头像", bool(row) and bool(row.get("avatar")))
        check("用户列表回显简介", bool(row) and row.get("description") == "用户创建简介")
        # 非法头像 400
        r = c.post(f"{BASE}/api/admin/users", headers=auth(adm),
                   json={"username": f"bad_{sfx}", "role_names": [], "avatar": "notdata"})
        check("非法头像创建被拒(400)", r.status_code == 400, str(r.status_code))
    p=sum(1 for _,x in ok if x); print(f"\n==== {p}/{len(ok)} passed ====")
    raise SystemExit(0 if p==len(ok) else 1)

if __name__=="__main__":
    main()
