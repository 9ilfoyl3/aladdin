"""Verify the fixed-role admin endpoints that back the management UI pages.

固定角色模型（tenant-rbac-refactor）下角色 CRUD / 权限点字典 / 用户角色分配端点均已删除。
本脚本只校验仍然存在的固定角色管理 UI 流程：
  - GET /api/auth/me 返回正确身份（is_super_admin / role）
  - 超管建租户 + 列租户（Tenants 页）
  - 租管建用户固定为 member（Users 页），列表/重置口令/启停可用
  - 超管下钻为租户新增管理员，得 role == admin
"""
from __future__ import annotations
import sys, uuid, httpx

BASE = "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False)
SFX = uuid.uuid4().hex[:6]
P, F = 0, 0
fails = []

def ck(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print(f"  [PASS] {name}")
    else:
        F += 1; fails.append(f"{name} :: {detail}"); print(f"  [FAIL] {name} -- {detail}")

def auth(t): return {"Authorization": f"Bearer {t}"}

# super admin: login + change pwd if needed
r = c.post("/api/auth/login", json={"username": "superadmin", "password": "ChangeMe!Admin2026"})
if r.status_code == 200 and r.json().get("must_change_password"):
    tok = r.json()["access_token"]
    c.post("/api/auth/change-password", headers=auth(tok),
           json={"old_password": "ChangeMe!Admin2026", "new_password": "SuperAdmin#New2026"})
r = c.post("/api/auth/login", json={"username": "superadmin", "password": "SuperAdmin#New2026"})
ck("super admin login", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
sa = r.json()["access_token"]

# super admin identity: is_super_admin=True, role=None
me = c.get("/api/auth/me", headers=auth(sa)).json()
ck("super admin me is_super_admin", me.get("is_super_admin") is True, str(me))
ck("super admin role is None", me.get("role") is None, str(me))

# create tenant (Tenants page)
r = c.post("/api/admin/tenants", headers=auth(sa),
           json={"name": f"法院X-{SFX}", "admin_username": f"adminx_{SFX}"})
ck("create tenant", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
tid = r.json()["id"]; admin_temp = r.json()["admin_temp_password"]

# list tenants (Tenants page)
r = c.get("/api/admin/tenants", headers=auth(sa))
ck("list tenants", r.status_code == 200 and any(t["id"] == tid for t in r.json()), r.text[:120])

# tenant admin login + change pwd
r = c.post("/api/auth/login", json={"username": f"adminx_{SFX}", "password": admin_temp, "tenant_id": tid})
atok = r.json()["access_token"]
c.post("/api/auth/change-password", headers=auth(atok),
       json={"old_password": admin_temp, "new_password": "AdminX#Pass2026"})
r = c.post("/api/auth/login", json={"username": f"adminx_{SFX}", "password": "AdminX#Pass2026", "tenant_id": tid})
atok = r.json()["access_token"]

# tenant admin identity: role == admin, not super
me = c.get("/api/auth/me", headers=auth(atok)).json()
ck("tenant admin role == admin", me.get("role") == "admin", str(me))
ck("tenant admin not super admin", me.get("is_super_admin") is False, str(me))

# create user (Users page) — 固定角色 member
r = c.post("/api/admin/users", headers=auth(atok),
           json={"username": f"ux_{SFX}"})
ck("create user", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
uid = r.json()["id"]
ck("created user role == member", r.json().get("role") == "member", str(r.json().get("role")))
ck("user temp pwd returned", bool(r.json().get("temp_password")), "")

# list users (Users page) — paginated PageResult
r = c.get("/api/admin/users", headers=auth(atok))
ck("list users", r.status_code == 200 and any(u["id"] == uid for u in r.json()["items"]), r.text[:120])
row = next((u for u in r.json()["items"] if u["id"] == uid), {}) if r.status_code == 200 else {}
ck("list user shows role member", row.get("role") == "member", str(row.get("role")))

# reset password (Users page)
r = c.post(f"/api/admin/users/{uid}/reset-password", headers=auth(atok))
ck("reset password", r.status_code == 200 and bool(r.json().get("temp_password")), r.text[:120])

# toggle user status (Users page)
r = c.put(f"/api/admin/users/{uid}/status", headers=auth(atok), json={"is_active": False})
ck("disable user", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
c.put(f"/api/admin/users/{uid}/status", headers=auth(atok), json={"is_active": True})

# 超管下钻为该租户新增管理员（得 role == admin）
r = c.post(f"/api/admin/tenants/{tid}/admins", headers=auth(sa),
           json={"username": f"adminx2_{SFX}"})
ck("super add tenant admin", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
ck("added tenant admin role == admin", r.status_code == 201 and r.json().get("role") == "admin",
   str(r.json().get("role") if r.status_code == 201 else "-"))

# tenant admin cannot access platform tenant mgmt (tenant 管理是平台级)
r = c.get("/api/admin/tenants", headers=auth(atok))
ck("tenant admin cannot list tenants -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

print("\n" + "=" * 50)
print(f"RESULT: {P} passed, {F} failed")
for x in fails: print("  - " + x)
sys.exit(1 if F else 0)
