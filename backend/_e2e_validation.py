"""Test input validation + privilege-escalation guard on the user/tenant system."""
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

print("== privilege-escalation guard (role perms) ==")
# tenant admin tries to create a role with platform perm tenant:manage -> 403
r = c.post("/api/admin/roles", headers=auth(atok),
           json={"name": f"evil_{SFX}", "permission_codes": ["tenant:manage"]})
ck("role with tenant:manage -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
# admin has all tenant perms, so creating role with config:manage is allowed (admin owns it)
r = c.post("/api/admin/roles", headers=auth(atok),
           json={"name": f"cfg_{SFX}", "permission_codes": ["config:manage", "kb:read"]})
ck("role with owned perms -> 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
cfg_role = r.json().get("id")

# Now create a limited user, give them role:manage only, and confirm they cannot grant config:manage they don't hold
# create a custom role 'rolemgr' with role:manage + menu:admin
r = c.post("/api/admin/roles", headers=auth(atok),
           json={"name": f"rolemgr_{SFX}", "permission_codes": ["role:manage", "menu:admin"]})
ck("create rolemgr role -> 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
rolemgr_id = r.json()["id"]
# create user with that role
r = c.post("/api/admin/users", headers=auth(atok),
           json={"username": f"rmgr_{SFX}", "role_names": [], "password": "RoleMgr#2026"})
ck("create rolemgr user -> 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
rmgr_uid = r.json()["id"]
c.put(f"/api/admin/users/{rmgr_uid}/roles", headers=auth(atok), json={"role_ids": [rolemgr_id]})
# that user logs in (password set directly, no forced change)
rmgr_tok = c.post("/api/auth/login", json={"username": f"rmgr_{SFX}", "password": "RoleMgr#2026", "tenant_id": tid}).json()["access_token"]
# rmgr has role:manage but NOT config:manage -> creating role with config:manage must be 403
r = c.post("/api/admin/roles", headers=auth(rmgr_tok),
           json={"name": f"esc_{SFX}", "permission_codes": ["config:manage"]})
ck("escalation: grant perm not owned -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
# rmgr CAN create a role with perms it owns (role:manage, menu:admin)
r = c.post("/api/admin/roles", headers=auth(rmgr_tok),
           json={"name": f"ok_{SFX}", "permission_codes": ["menu:admin"]})
ck("non-escalation: grant owned perm -> 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")

print("\n" + "=" * 50)
print(f"RESULT: {P} passed, {F} failed")
for x in fails: print("  - " + x)
sys.exit(1 if F else 0)
