"""验证：邀请注册时可带可选头像与简介（建租户邀请 + 建用户邀请）。"""
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
        # 超管发"建租户"邀请
        inv = c.post(f"{BASE}/api/admin/invitations", headers=auth(sa),
                     json={"scope": "create_tenant", "expires_in_hours": 168}).json()
        # 接受（带头像+简介，建租户+自身为管理员）
        admu = f"invadm_{sfx}"
        r = c.post(f"{BASE}/api/invitations/{inv['token']}/accept",
                   json={"username": admu, "password": "InvPwd123", "tenant_name": f"邀请企业_{sfx}",
                         "description": "邀请注册的管理员", "avatar": PNG})
        check("建租户邀请接受 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        # 该管理员登录看自己资料
        tok = c.post(f"{BASE}/api/auth/login", json={"username": admu, "password": "InvPwd123"}).json()["access_token"]
        prof = c.get(f"{BASE}/api/auth/me/profile", headers=auth(tok)).json()
        check("管理员简介已写入", prof.get("description") == "邀请注册的管理员", str(prof.get("description")))
        check("管理员头像已写入", bool(prof.get("avatar")))

        # 该管理员发"建用户"邀请
        inv2 = c.post(f"{BASE}/api/admin/invitations", headers=auth(tok),
                      json={"scope": "create_user", "expires_in_hours": 168}).json()
        usr = f"invusr_{sfx}"
        r = c.post(f"{BASE}/api/invitations/{inv2['token']}/accept",
                   json={"username": usr, "password": "InvPwd456",
                         "description": "邀请注册的用户", "avatar": PNG})
        check("建用户邀请接受 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        tok2 = c.post(f"{BASE}/api/auth/login", json={"username": usr, "password": "InvPwd456"}).json()["access_token"]
        prof2 = c.get(f"{BASE}/api/auth/me/profile", headers=auth(tok2)).json()
        check("用户简介已写入", prof2.get("description") == "邀请注册的用户", str(prof2.get("description")))
        check("用户头像已写入", bool(prof2.get("avatar")))

        # 不带头像/简介也应正常（可选）
        inv3 = c.post(f"{BASE}/api/admin/invitations", headers=auth(tok),
                      json={"scope": "create_user", "expires_in_hours": 168}).json()
        r = c.post(f"{BASE}/api/invitations/{inv3['token']}/accept",
                   json={"username": f"plain_{sfx}", "password": "InvPwd789"})
        check("不带头像/简介接受也正常 200", r.status_code == 200, f"{r.status_code}")
    p=sum(1 for _,x in ok if x); print(f"\n==== {p}/{len(ok)} passed ====")
    raise SystemExit(0 if p==len(ok) else 1)

if __name__=="__main__":
    main()
