"""Test input validation + fixed-role privilege guard on the user/tenant system.

固定角色模型（tenant-rbac-refactor）下角色 CRUD / 权限点字典已删除，越权升级面收敛为：
租管经 POST /api/admin/users 建号恒为 member —— UserCreate 无角色字段，租管在 API 层
根本无法把用户设为 admin（设立 admin 仅经平台流程）。本脚本保留用户名/口令校验，
并以「建号恒 member」+「角色端点已删（404）」校验固定角色的越权防线。
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
    if cond: P += 1; print(f"  [PASS] {name}")
    else: F += 1; fails.append(f"{name}::{detail}"); print(f"  [FAIL] {name} -- {detail}")

def auth(t): return {"Authorization": f"Bearer {t}"}

# super admin
r = c.post("/api/auth/login", json={"username": "superadmin", "password": "ChangeMe!Admin2026"})
if r.status_code == 200 and r.json().get("must_change_password"):
    t = r.json()["access_token"]
    c.post("/api/auth/change-password", headers=auth(t),
           json={"old_password": "ChangeMe!Admin2026", "new_password": "SuperAdmin#New2026"})
sa = c.post("/api/auth/login", json={"username": "superadmin", "password": "SuperAdmin#New2026"}).json()["access_token"]

print("== username validation ==")
# too short
r = c.post("/api/admin/tenants", headers=auth(sa), json={"name": f"T-{SFX}", "admin_username": "ab"})
ck("username too short -> 400", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
# illegal chars
r = c.post("/api/admin/tenants", headers=auth(sa), json={"name": f"T-{SFX}", "admin_username": "bad name!"})
ck("username illegal char -> 400", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
# overly long (>32)
r = c.post("/api/admin/tenants", headers=auth(sa), json={"name": f"T-{SFX}", "admin_username": "a" * 40})
ck("username too long -> 400", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
# tenant name too long
r = c.post("/api/admin/tenants", headers=auth(sa), json={"name": "n" * 80, "admin_username": f"okadmin_{SFX}"})
ck("tenant name too long -> 400", r.status_code == 400, f"{r.status_code} {r.text[:100]}")

# valid tenant for further tests
r = c.post("/api/admin/tenants", headers=auth(sa), json={"name": f"法院V-{SFX}", "admin_username": f"adminv_{SFX}"})
ck("valid tenant create -> 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
tid = r.json()["id"]; atemp = r.json()["admin_temp_password"]

print("== password validation (change-password) ==")
# admin login
atok = c.post("/api/auth/login", json={"username": f"adminv_{SFX}", "password": atemp, "tenant_id": tid}).json()["access_token"]
# weak: too short
r = c.post("/api/auth/change-password", headers=auth(atok), json={"old_password": atemp, "new_password": "ab1"})
ck("pwd too short -> 400", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
# weak: no digit
r = c.post("/api/auth/change-password", headers=auth(atok), json={"old_password": atemp, "new_password": "onlyletters"})
ck("pwd no digit -> 400", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
# overly long (>64)
r = c.post("/api/auth/change-password", headers=auth(atok), json={"old_password": atemp, "new_password": "a1" * 40})
ck("pwd too long -> 400", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
# valid
r = c.post("/api/auth/change-password", headers=auth(atok), json={"old_password": atemp, "new_password": "AdminV#Pass2026"})
ck("valid pwd change -> 200", r.status_code == 200, f"{r.status_code} {r.text[:100]}")
atok = c.post("/api/auth/login", json={"username": f"adminv_{SFX}", "password": "AdminV#Pass2026", "tenant_id": tid}).json()["access_token"]

print("== fixed-role privilege guard ==")
# 租管建号固定 member：UserCreate 无角色字段，无法在 API 层造 admin（越权升级面已封）
r = c.post("/api/admin/users", headers=auth(atok),
           json={"username": f"member1_{SFX}", "password": "Member#2026"})
ck("admin create user -> 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
ck("created user role == member (不可越权造 admin)",
   r.status_code == 201 and r.json().get("role") == "member",
   str(r.json().get("role") if r.status_code == 201 else "-"))

# 角色管理端点已删除：任何人访问 -> 404（记录移除事实）
r = c.get("/api/admin/roles", headers=auth(atok))
ck("role CRUD endpoint removed (GET /admin/roles -> 404)", r.status_code == 404, f"{r.status_code} {r.text[:100]}")
r = c.get("/api/admin/permissions", headers=auth(atok))
ck("permission dict endpoint removed (GET /admin/permissions -> 404)", r.status_code == 404, f"{r.status_code} {r.text[:100]}")

print("\n" + "=" * 50)
print(f"RESULT: {P} passed, {F} failed")
for x in fails: print("  - " + x)
sys.exit(1 if F else 0)
